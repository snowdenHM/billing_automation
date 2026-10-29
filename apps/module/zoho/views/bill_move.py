"""Move an uploaded bill from one Zoho voucher type to another.

A bill lands in the wrong module all the time: a purchase bill uploaded
to Sales, a payment advice filed as a bill. The document is already
uploaded and already analysed, so the point of a move is to keep that
work — re-uploading means paying for the analysis twice and losing
whatever the operator has mapped by hand.

Which is why the old implementation was the problem. It copied a short
hand-written list of fields per module, and everything outside that list
was dropped on the floor without a word. The vendor selection was not on
any of those lists, so **every move lost the vendor mapping** — the most
expensive field on the bill, and the whole thing the feature claims to
preserve. So were the duplicate-detection hash, the journal's ledger
mapping and the TDS selection.

Here the carry-over is derived from the models instead: every field the
two sides share moves across, and the handful that must *not* move are
named once, in ``NEVER_COPY``. A field added to a bill model later is
carried by default rather than silently forgotten.

What genuinely cannot move is reported rather than hidden. Payment and
receipt vouchers hold no line items, so moving a bill into one discards
its lines; the response and the activity entry both say how many.
"""

import logging

from django.db import IntegrityError, transaction
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common import roles
from apps.common.services.activity import record_activity
from apps.common.models import BillActivity
from apps.common.permissions import HasOrgPermission, requires

from ..models import (
    ExpenseBill, ExpenseZohoBill, ExpenseZohoConsolidatedProduct,
    ExpenseZohoProduct, JournalBill, JournalZohoBill,
    JournalZohoConsolidatedProduct, JournalZohoProduct, PaymentBill,
    PaymentZohoBill, ReceiptBill, ReceiptZohoBill, SalesBill, SalesZohoBill,
    SalesZohoConsolidatedProduct, SalesZohoProduct, VendorBill, VendorZohoBill,
    VendorZohoConsolidatedProduct, VendorZohoProduct,
)
from .helpers import get_organization_from_request

logger = logging.getLogger(__name__)


class _Module:
    """One voucher type's models, and the party field it maps to."""

    def __init__(self, label, bill, analysed, party_field,
                 product=None, consolidated=None):
        self.label = label
        self.bill = bill
        self.analysed = analysed
        self.party_field = party_field
        self.product = product
        self.consolidated = consolidated

    @property
    def holds_line_items(self):
        return self.product is not None


MODULES = {
    "vendor": _Module(
        "Purchase Voucher", VendorBill, VendorZohoBill, "vendor",
        VendorZohoProduct, VendorZohoConsolidatedProduct,
    ),
    "expense": _Module(
        "Expense", ExpenseBill, ExpenseZohoBill, "vendor",
        ExpenseZohoProduct, ExpenseZohoConsolidatedProduct,
    ),
    "journal": _Module(
        "Journal Voucher", JournalBill, JournalZohoBill, "vendor",
        JournalZohoProduct, JournalZohoConsolidatedProduct,
    ),
    "sales": _Module(
        "Sales Invoice", SalesBill, SalesZohoBill, "customer",
        SalesZohoProduct, SalesZohoConsolidatedProduct,
    ),
    # No line items of their own: a voucher records an amount paid or
    # received, not what was bought.
    "payment": _Module("Payment Voucher", PaymentBill, PaymentZohoBill, "vendor"),
    "receipt": _Module("Receipt Voucher", ReceiptBill, ReceiptZohoBill, "customer"),
}

MOVABLE_STATUSES = ("Draft", "Analysed")

#: Never carried across, whatever both models happen to share.
#:
#: ``id`` and the timestamps identify the row, not the document.
#: ``selectBill``/``zohoBill``/``organization`` are set explicitly.
#: The trash columns belong to the row that was trashed.
#: ``zoho_bill_id`` is the id Zoho gave the *old* voucher — carrying it
#: would make the new bill look already synced and point at a voucher
#: built from different data.
NEVER_COPY = frozenset({
    "id", "created_at", "update_at", "updated_at",
    "is_deleted", "deleted_at", "deleted_by",
    "selectBill", "zohoBill", "organization",
    "zoho_bill_id",
})


def _carryable_fields(source_model, target_model):
    """Concrete fields both models hold, minus the ones that must not move."""
    def names(model):
        return {
            f.name for f in model._meta.get_fields()
            if hasattr(f, "attname") and not f.auto_created
        }

    return (names(source_model) & names(target_model)) - NEVER_COPY


def _copy_values(source, target_model, extra_skip=()):
    """The field values to carry from ``source`` into ``target_model``."""
    fields = _carryable_fields(type(source), target_model) - set(extra_skip)
    return {name: getattr(source, name) for name in fields}


def _move_one(bill_id, source, dest, organization, actor):
    """Move a single bill. Raises on anything it cannot complete.

    Raising is deliberate. The previous version caught failures while
    copying line items, logged a warning and carried on to delete the
    source — so a bill could arrive in its new module with no lines at
    all and the original gone, with nothing on screen to say the
    analysis had been thrown away.
    """
    try:
        source_bill = source.bill.objects.alive().get(
            id=bill_id, organization=organization,
        )
    except source.bill.DoesNotExist:
        raise ValueError(f"No {source.label} with id {bill_id} in this workspace")

    if source_bill.status not in MOVABLE_STATUSES:
        raise ValueError(
            f"Only {' and '.join(MOVABLE_STATUSES)} bills can be moved. "
            f"This one is {source_bill.status}."
        )

    # A worker holding this bill's id would write its result into a row
    # that no longer exists, and the analysis would be lost with no
    # error anybody sees.
    if getattr(source_bill, "is_processing", False):
        raise ValueError(
            "This bill is being analysed right now. Wait for it to "
            "finish, then move it."
        )

    source_analysed = source.analysed.objects.filter(
        selectBill=source_bill, organization=organization,
    ).first()

    bill_values = _copy_values(source_bill, dest.bill)

    # The name is the document's identity and should survive the move.
    # It can only be kept if the destination does not already hold it;
    # when it does, leaving it blank lets the model mint a fresh one
    # rather than failing the move over a label.
    renamed = False
    name = bill_values.get("billmunshiName")
    if name and dest.bill.objects.filter(
        organization=organization, billmunshiName=name,
    ).exists():
        bill_values["billmunshiName"] = None
        renamed = True

    new_bill = dest.bill.objects.create(organization=organization, **bill_values)

    lines_dropped = 0
    party_dropped = None
    new_analysed = None

    if source_analysed:
        # The party is a vendor on the purchase side and a customer on
        # the sales side; they are different tables, so a cross-side
        # move cannot carry it. Say which one was dropped instead of
        # leaving the operator to notice an empty field.
        skip = ()
        if source.party_field != dest.party_field:
            skip = (source.party_field, dest.party_field)
            if getattr(source_analysed, source.party_field, None):
                party_dropped = source.party_field

        analysed_values = _copy_values(source_analysed, dest.analysed, extra_skip=skip)
        new_analysed = dest.analysed.objects.create(
            selectBill=new_bill, organization=organization, **analysed_values,
        )

        for relation, model_attr in (
            ("products", "product"),
            ("consolidated_products", "consolidated"),
        ):
            rows = list(getattr(source_analysed, relation).all()) \
                if hasattr(source_analysed, relation) else []
            target_model = getattr(dest, model_attr)

            if target_model is None:
                lines_dropped += len(rows)
                continue

            for row in rows:
                target_model.objects.create(
                    zohoBill=new_analysed, organization=organization,
                    **_copy_values(row, target_model),
                )

    # The source row goes. The document itself is untouched: the file
    # path was carried over, and deleting a bill row does not delete
    # its stored file.
    if source_analysed:
        source_analysed.delete()
    source_bill.delete()

    detail = {
        "from": source.label,
        "to": dest.label,
        "previous_bill_id": str(bill_id),
    }
    if lines_dropped:
        detail["line_items_dropped"] = lines_dropped
    if party_dropped:
        detail["party_cleared"] = party_dropped
    if renamed:
        detail["renamed_to"] = new_bill.billmunshiName

    # Recorded against the bill that survives: the source row is gone,
    # so the old id lives in the metadata to tie the two halves.
    record_activity(
        new_bill, BillActivity.Action.MOVED,
        actor=actor, organization=organization,
        summary=f"Moved from {source.label} to {dest.label}"[:255],
        metadata=detail,
    )

    return new_bill, lines_dropped, party_dropped, renamed


@extend_schema(
    request={
        "application/json": {
            "type": "object",
            "properties": {
                "from": {"type": "string", "enum": sorted(MODULES)},
                "to": {"type": "string", "enum": sorted(MODULES)},
                "bill_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["from", "to", "bill_ids"],
        }
    },
    responses={
        200: {"description": "Move completed, possibly with per-bill failures"},
        400: {"description": "Invalid request, or every bill failed to move"},
        404: {"description": "Organization not found"},
    },
    tags=["Zoho Bill Management"],
)
@requires(roles.MOVE_BILLS)
@api_view(["POST"])
@permission_classes([IsAuthenticated, HasOrgPermission])
def move_bill_between_modules_view(request, org_id):
    """Move bills between voucher types, keeping the analysis."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response(
            {"detail": "Organization not found"},
            status=status.HTTP_404_NOT_FOUND,
        )

    from_module = str(request.data.get("from") or "").lower()
    to_module = str(request.data.get("to") or "").lower()
    bill_ids = request.data.get("bill_ids") or []

    if from_module not in MODULES or to_module not in MODULES:
        return Response({
            "error": "Invalid Module",
            "detail": f"'from' and 'to' must each be one of: {', '.join(sorted(MODULES))}",
            "received": {"from": from_module, "to": to_module},
        }, status=status.HTTP_400_BAD_REQUEST)

    if from_module == to_module:
        return Response({
            "error": "Same Module",
            "detail": "A bill cannot be moved to the module it is already in.",
        }, status=status.HTTP_400_BAD_REQUEST)

    if not isinstance(bill_ids, list) or not bill_ids:
        return Response({
            "error": "Missing Bill IDs",
            "detail": "bill_ids must be a non-empty array of bill IDs.",
        }, status=status.HTTP_400_BAD_REQUEST)

    source, dest = MODULES[from_module], MODULES[to_module]

    moved, errors = [], []
    total_lines_dropped = 0

    for bill_id in bill_ids:
        try:
            # Per bill, so one bad bill in a selection of twenty does
            # not roll back the nineteen that moved cleanly.
            with transaction.atomic():
                new_bill, dropped, party_dropped, renamed = _move_one(
                    bill_id, source, dest, organization, request.user,
                )
        except ValueError as exc:
            errors.append({"bill_id": str(bill_id), "error": str(exc)})
            continue
        except IntegrityError as exc:
            logger.warning("Move of bill %s failed: %s", bill_id, exc)
            errors.append({
                "bill_id": str(bill_id),
                "error": "The destination module rejected this bill.",
            })
            continue
        except Exception:
            logger.exception("Move of bill %s from %s to %s failed",
                             bill_id, from_module, to_module)
            errors.append({
                "bill_id": str(bill_id),
                "error": "Could not move this bill. Check the server logs.",
            })
            continue

        total_lines_dropped += dropped
        moved.append({
            "original_bill_id": str(bill_id),
            "new_bill_id": str(new_bill.id),
            "name": new_bill.billmunshiName,
            "line_items_dropped": dropped,
            "party_cleared": party_dropped,
            "renamed": renamed,
        })

    payload = {
        "total_bills": len(bill_ids),
        "successful_moves": len(moved),
        "failed_moves": len(errors),
        "moved_bills": moved,
        "from_module": from_module,
        "to_module": to_module,
        "errors": errors,
    }

    # Said plainly, because it is the one consequence an operator cannot
    # see on the destination screen.
    if total_lines_dropped:
        payload["warning"] = (
            f"A {dest.label} holds no line items, so {total_lines_dropped} "
            f"line(s) were not carried over."
        )

    if errors and not moved:
        payload["error"] = "Move Failed"
        payload["message"] = f"None of the {len(errors)} bill(s) could be moved."
        return Response(payload, status=status.HTTP_400_BAD_REQUEST)

    payload["message"] = (
        f"Moved {len(moved)} bill(s) to {dest.label}"
        + (f"; {len(errors)} could not be moved." if errors else ".")
    )
    return Response(payload)

"""Recording and reading the bill activity log.

Every call here is best-effort. An audit trail that can raise is worse
than no audit trail: it would turn a logged side-note into a failed
upload or a failed verify. Everything is wrapped, and a failure to record
is logged and swallowed.
"""

import logging
from decimal import Decimal, InvalidOperation

from django.db import transaction

from apps.common.models import BillActivity

logger = logging.getLogger(__name__)


# Model class name -> (module, bill_type). Keyed by name rather than by
# importing the models so this module stays free of app-loading order
# problems and can be imported from anywhere.
_BILL_KINDS = {
    "TallyVendorBill": (BillActivity.Module.TALLY, BillActivity.BillType.VENDOR),
    "TallyExpenseBill": (BillActivity.Module.TALLY, BillActivity.BillType.EXPENSE),
    "TallyPaymentBill": (BillActivity.Module.TALLY, BillActivity.BillType.PAYMENT),
    "TallySalesBill": (BillActivity.Module.TALLY, BillActivity.BillType.SALES),
    "TallyReceiptBill": (BillActivity.Module.TALLY, BillActivity.BillType.RECEIPT),
    "VendorBill": (BillActivity.Module.ZOHO, BillActivity.BillType.VENDOR),
    "ExpenseBill": (BillActivity.Module.ZOHO, BillActivity.BillType.EXPENSE),
    "JournalBill": (BillActivity.Module.ZOHO, BillActivity.BillType.JOURNAL),
    # The Zoho models carry no "Zoho" prefix, so these names are the Zoho
    # sales/receipt/payment types, not the Tally ones above.
    "SalesBill": (BillActivity.Module.ZOHO, BillActivity.BillType.SALES),
    "ReceiptBill": (BillActivity.Module.ZOHO, BillActivity.BillType.RECEIPT),
    "PaymentBill": (BillActivity.Module.ZOHO, BillActivity.BillType.PAYMENT),
}


def resolve_bill_kind(bill):
    """Return ``(module, bill_type)`` for a bill instance, or ``(None, None)``."""
    return _BILL_KINDS.get(type(bill).__name__, (None, None))


def _actor_label(user):
    if user is None or not getattr(user, "is_authenticated", False):
        return "Bill Munshi"
    name = (getattr(user, "get_full_name", lambda: "")() or "").strip()
    return (name or getattr(user, "email", "") or "User")[:255]


def _bill_name(bill):
    return (
        getattr(bill, "bill_munshi_name", None)
        or getattr(bill, "billmunshiName", None)
        or str(getattr(bill, "pk", ""))
    )


def record_activity(
    bill,
    action,
    *,
    actor=None,
    summary="",
    changes=None,
    metadata=None,
    organization=None,
):
    """Append one event for *bill*. Never raises.

    *actor* is the request user, or None for anything a background job
    did. *changes* is a list of ``{"field", "from", "to"}`` dicts, each
    optionally carrying ``"suggested"`` when the system proposed a value
    the user did not take.
    """
    try:
        module, bill_type = resolve_bill_kind(bill)
        if module is None:
            logger.warning(
                "No activity mapping for %s; event %s not recorded",
                type(bill).__name__, action,
            )
            return None

        org = organization or getattr(bill, "organization", None)
        if org is None:
            return None

        # Its own savepoint. Callers log from inside their transaction
        # (verify, move), and a database error that we then swallow would
        # leave that transaction unusable -- every later query in the
        # request would fail with TransactionManagementError. The
        # savepoint confines the damage to this insert.
        with transaction.atomic():
            return BillActivity.objects.create(
                organization=org,
                module=module,
                bill_type=bill_type,
                bill_id=bill.pk,
                action=action,
                actor=actor if getattr(actor, "is_authenticated", False) else None,
                actor_label=_actor_label(actor),
                summary=(summary or "")[:255],
                changes=changes or [],
                metadata=metadata or {},
            )
    except Exception:
        logger.exception(
            "Could not record %s activity for bill %s",
            action, getattr(bill, "pk", "?"),
        )
        return None


def activity_for_bill(bill):
    """Timeline for one bill, newest first."""
    return BillActivity.objects.filter(bill_id=bill.pk).select_related("actor")


# ---------------------------------------------------------------------------
# Change sets
# ---------------------------------------------------------------------------

def _as_text(value):
    if value is None:
        return ""
    return str(value)


def _as_number(text):
    """*text* as a finite Decimal, or None when it is not a number.

    Thousands separators are dropped first: a total the UI renders as
    ``1,20,000.00`` and the model stores as ``120000`` is the same
    figure written two ways, not an edit.
    """
    candidate = text.strip().replace(",", "").replace(" ", "")
    if not candidate:
        return None
    try:
        number = Decimal(candidate)
    except (InvalidOperation, ValueError):
        return None
    # NaN compares unequal to itself and Infinity is never a real
    # amount; treat both as "not a number" so they fall back to text.
    return number if number.is_finite() else None


def same_value(old, new):
    """True when the two rendered values mean the same thing.

    The corrections recorded here are the only ground truth we have for
    how well the extraction routes read a bill, so an entry that is not
    a correction poisons that signal. Two amounts are the same amount
    whichever way they are written -- ``0`` and ``0.00``, ``1200`` and
    ``1,200.00`` -- and the verify screen reformats fields it never
    changed. Everything else is compared as text, deliberately: a vendor
    renamed from ``ACME`` to ``Acme`` is a real correction.
    """
    if old == new:
        return True
    old_number, new_number = _as_number(old), _as_number(new)
    if old_number is not None and new_number is not None:
        return old_number == new_number
    return old.strip() == new.strip()


def diff_fields(before: dict, after: dict, *, suggested: dict = None, labels: dict = None):
    """Change entries for the keys whose value differs between the two dicts.

    *labels* renames a field for display. *suggested* carries what the
    system had proposed for a field, which is what makes an entry useful
    for learning rather than only for audit.

    Values that differ only in how they are written are not changes --
    see :func:`same_value`.
    """
    suggested = suggested or {}
    labels = labels or {}
    changes = []
    for field in before.keys() | after.keys():
        old, new = _as_text(before.get(field)), _as_text(after.get(field))
        if same_value(old, new):
            continue
        entry = {
            "field": labels.get(field, field),
            "from": old,
            "to": new,
        }
        if field in suggested:
            proposed = _as_text(suggested[field])
            # Same test again: a suggestion the user accepted and merely
            # reformatted was taken, and recording it as refused would
            # count against the route that got it right.
            if proposed and not same_value(proposed, new):
                entry["suggested"] = proposed
        changes.append(entry)
    return sorted(changes, key=lambda c: c["field"])


# Bill-level fields worth showing in a timeline. Line-item edits are
# summarised by count rather than diffed row by row -- a voucher can carry
# dozens of lines and a per-line diff would bury the figures that matter.
SNAPSHOT_FIELDS = (
    "bill_no", "bill_date", "due_date", "total",
    "igst", "cgst", "sgst", "tds", "round_off", "gst_type",
    # Payment-shaped vouchers (Zoho receipt/payment) carry the money in
    # ``amount`` and have no line items, so without these two a verify on
    # one of them records "no changes" however much the operator edited.
    "amount", "payment_mode",
)

BILL_FIELD_LABELS = {
    "bill_no": "Invoice number",
    "bill_date": "Bill date",
    "due_date": "Due date",
    "total": "Total",
    "igst": "IGST",
    "cgst": "CGST",
    "sgst": "SGST",
    "tds": "TDS",
    "round_off": "Round off",
    "gst_type": "GST type",
    "vendor": "Vendor",
    "customer": "Customer",
    "amount": "Amount",
    "payment_mode": "Payment mode",
    "line_items": "Line items",
}


def snapshot_bill_fields(analyzed_bill):
    """Readable values of the fields a verify is allowed to change.

    Missing attributes are skipped rather than defaulted, so a field a
    model does not have never shows up as an edit from blank.
    """
    if analyzed_bill is None:
        return {}
    snap = {}
    for field in SNAPSHOT_FIELDS:
        if hasattr(analyzed_bill, field):
            snap[field] = _as_text(getattr(analyzed_bill, field))
    # The party sits under a different name on each side of the ledger:
    # ``vendor`` on a purchase, ``customer`` on a sale or receipt. Both are
    # snapshotted, so changing the party is recorded whichever it is.
    for party_field in ("vendor", "customer"):
        party = getattr(analyzed_bill, party_field, None)
        if party is not None:
            snap[party_field] = _as_text(getattr(party, "name", party))
    # A payment-shaped voucher keeps its money in ``amount`` and exposes
    # ``total`` as a read-only mirror for the report builders. Snapshotting
    # both would report every edit to the figure twice, under two names.
    if "amount" in snap and "total" in snap and same_value(snap["amount"], snap["total"]):
        del snap["total"]

    try:
        snap["line_items"] = str(analyzed_bill.products.count())
    except Exception:
        pass
    return snap

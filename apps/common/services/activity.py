"""Recording and reading the bill activity log.

Every call here is best-effort. An audit trail that can raise is worse
than no audit trail: it would turn a logged side-note into a failed
upload or a failed verify. Everything is wrapped, and a failure to record
is logged and swallowed.
"""

import logging

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


def diff_fields(before: dict, after: dict, *, suggested: dict = None, labels: dict = None):
    """Change entries for the keys whose value differs between the two dicts.

    *labels* renames a field for display. *suggested* carries what the
    system had proposed for a field, which is what makes an entry useful
    for learning rather than only for audit.
    """
    suggested = suggested or {}
    labels = labels or {}
    changes = []
    for field in before.keys() | after.keys():
        old, new = _as_text(before.get(field)), _as_text(after.get(field))
        if old == new:
            continue
        entry = {
            "field": labels.get(field, field),
            "from": old,
            "to": new,
        }
        if field in suggested:
            proposed = _as_text(suggested[field])
            if proposed and proposed != new:
                entry["suggested"] = proposed
        changes.append(entry)
    return sorted(changes, key=lambda c: c["field"])


# Bill-level fields worth showing in a timeline. Line-item edits are
# summarised by count rather than diffed row by row -- a voucher can carry
# dozens of lines and a per-line diff would bury the figures that matter.
SNAPSHOT_FIELDS = (
    "bill_no", "bill_date", "due_date", "total",
    "igst", "cgst", "sgst", "tds", "round_off", "gst_type",
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
    vendor = getattr(analyzed_bill, "vendor", None)
    if vendor is not None:
        snap["vendor"] = _as_text(getattr(vendor, "name", vendor))
    try:
        snap["line_items"] = str(analyzed_bill.products.count())
    except Exception:
        pass
    return snap

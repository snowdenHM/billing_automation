"""
Polling-based notifications endpoint (see #16 in the upload audit).

The frontend can't afford a WebSocket/push channel yet, but users
want to know when a background bill-analysis job finishes without
having to refresh the list manually. This endpoint returns any bill
that transitioned into a "user-visible" state since ``?since=<iso>``:

  * analysis complete    → Analysed / Verified
  * analysis errored     → is_processing=False + processing_error
  * duplicate detected   → is_duplicate=True

The frontend polls this every ~15s and shows a bell-icon dropdown.
"""
from datetime import datetime, timezone as dt_timezone

from django.utils.dateparse import parse_datetime
from rest_framework import status as http_status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.permissions import IsOrgAdmin

# Local imports done lazily inside the view to avoid pulling every
# module (tally, zoho) at Django import time.


def _to_iso(dt):
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=dt_timezone.utc)
    return dt.isoformat()


def _classify_bill(bill):
    """Return a (kind, message) tuple for the frontend notification card."""
    if bill.is_duplicate:
        return "duplicate", (
            f"'{getattr(bill, 'bill_munshi_name', None) or getattr(bill, 'billmunshiName', 'Bill')}' "
            f"flagged as a possible duplicate."
        )
    if getattr(bill, "processing_error", None):
        return "error", (
            f"Analysis failed for "
            f"'{getattr(bill, 'bill_munshi_name', None) or getattr(bill, 'billmunshiName', 'Bill')}': "
            f"{bill.processing_error}"
        )
    return "done", (
        f"Analysis complete for "
        f"'{getattr(bill, 'bill_munshi_name', None) or getattr(bill, 'billmunshiName', 'Bill')}'."
    )


def _collect_from(model_cls, org_id, since_dt, module_label):
    """Bills updated since ``since_dt`` that qualify as notifications."""
    qs = model_cls.objects.filter(organization_id=org_id, updated_at__gte=since_dt)
    # Only surface bills that are in a user-visible state — either
    # analysis has produced data (``analysed_data`` non-empty) or an
    # error was recorded, or the row was flagged as duplicate.
    notifications = []
    for bill in qs.only(
        "id", "updated_at", "is_duplicate", "is_processing",
        "processing_error", "status",
    ).iterator():
        # Skip drafts still being processed with no result yet.
        if bill.is_processing and not bill.processing_error:
            continue
        # Skip untouched Draft rows the poll picked up because
        # ``updated_at`` fires on any save.
        if bill.status == "Draft" and not bill.processing_error and not bill.is_duplicate:
            continue

        # Reload the full row for message building — the ``.only()`` above
        # kept the query small; getattr on unloaded fields would refetch
        # anyway, so this is cheaper as one round trip.
        full = model_cls.objects.get(pk=bill.id)
        kind, message = _classify_bill(full)
        notifications.append({
            "id": f"{module_label}-{bill.id}",
            "module": module_label,
            "bill_id": str(bill.id),
            "kind": kind,
            "message": message,
            "updated_at": _to_iso(bill.updated_at),
        })
    return notifications


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def poll_notifications(request, org_id):
    """
    ``GET /api/v1/orgs/<org_id>/notifications/?since=<iso>&modules=tally,zoho``

    Returns any bill-level notification (analysis done / errored /
    duplicate detected) that changed since the ``since`` cursor.
    Defaults to the last 5 minutes when no cursor is passed.
    """
    from datetime import timedelta

    from django.utils import timezone

    since_param = request.query_params.get("since")
    since_dt = parse_datetime(since_param) if since_param else None
    if since_dt is None:
        since_dt = timezone.now() - timedelta(minutes=5)
    if since_dt.tzinfo is None:
        since_dt = since_dt.replace(tzinfo=dt_timezone.utc)

    modules_param = request.query_params.get("modules", "tally,zoho")
    requested = {m.strip().lower() for m in modules_param.split(",") if m.strip()}

    notifications = []

    if "tally" in requested:
        from apps.module.tally.models import TallyExpenseBill, TallyVendorBill
        notifications.extend(_collect_from(TallyVendorBill, org_id, since_dt, "tally-vendor"))
        notifications.extend(_collect_from(TallyExpenseBill, org_id, since_dt, "tally-expense"))

    if "zoho" in requested:
        from apps.module.zoho.models import ExpenseBill, JournalBill, VendorBill
        notifications.extend(_collect_from(VendorBill, org_id, since_dt, "zoho-vendor"))
        notifications.extend(_collect_from(ExpenseBill, org_id, since_dt, "zoho-expense"))
        notifications.extend(_collect_from(JournalBill, org_id, since_dt, "zoho-journal"))

    notifications.sort(key=lambda n: n["updated_at"] or "", reverse=True)
    # Cap so a client that hasn't polled in a while doesn't blow the response.
    notifications = notifications[:100]

    return Response({
        "server_time": _to_iso(datetime.now(dt_timezone.utc)),
        "count": len(notifications),
        "notifications": notifications,
    }, status=http_status.HTTP_200_OK)

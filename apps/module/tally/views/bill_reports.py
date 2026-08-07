"""
XLSX report endpoints for Tally bills — Purchase Voucher, Journal Entry,
Payment Voucher. One endpoint per bill type; each accepts an optional
``?status=Analysed|Verified|Synced`` filter (default: all three).

The generated workbook matches the format the operations team uses in
Google Sheets — see :mod:`apps.common.services.reports`.
"""
import logging

from django.utils.timezone import now as tz_now
from drf_spectacular.utils import extend_schema
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.permissions import IsOrgAdmin
from apps.common.services.reports import (
    build_report_workbook,
    rows_from_expense_analyzed,
    rows_from_vendor_analyzed,
    workbook_to_response,
)
from apps.common.utils import get_organization_from_request

from ..models import (
    TallyExpenseAnalyzedBill,
    TallyExpenseBill,
    TallyPaymentAnalyzedBill,
    TallyPaymentBill,
    TallyVendorAnalyzedBill,
    TallyVendorBill,
)

logger = logging.getLogger(__name__)


_ALLOWED_STATUSES = {"Analysed", "Verified", "Synced"}
_DEFAULT_STATUSES = ["Analysed", "Verified", "Synced"]


def _resolve_statuses(request) -> list[str]:
    """Parse ?status=Analysed[,Verified,...] with sane defaults + whitelist."""
    raw = request.query_params.get("status", "")
    if not raw:
        return _DEFAULT_STATUSES
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    filtered = [p for p in parts if p in _ALLOWED_STATUSES]
    return filtered or _DEFAULT_STATUSES


def _org_not_found_response(org_id):
    return Response(
        {
            "error": "Organization Access Denied",
            "message": f"Organization with ID {org_id} not found or you do not have access to it.",
            "error_code": "ORG_NOT_FOUND",
        },
        status=404,
    )


def _build_and_stream(
    request,
    org_id,
    *,
    bill_model,
    analyzed_model,
    analyzed_bill_related_name,
    row_builder,
    filename_stub,
    sheet_title,
):
    """Common report handler.

    Loads bills matching the requested statuses, resolves each one's
    analysed header via ``analyzed_bill_related_name`` (falls back to
    the older ``analysed_headers`` reverse-FK), delegates row assembly
    to ``row_builder``, then returns an ``.xlsx`` HTTP response.
    """
    organization = get_organization_from_request(request, org_id)
    if not organization:
        return _org_not_found_response(org_id)

    statuses = _resolve_statuses(request)
    bills = (
        bill_model.objects.alive()
        .filter(organization=organization, status__in=statuses)
        .order_by("-created_at")
    )

    rows = []
    for bill in bills.iterator():
        analyzed = None
        # Try the common ``.analysed_headers.first()`` reverse manager.
        rel = getattr(bill, analyzed_bill_related_name, None)
        if rel is not None:
            try:
                analyzed = rel.order_by("-created_at").first()
            except Exception:
                analyzed = None
        if analyzed is None:
            # Explicit lookup for defensiveness.
            analyzed = analyzed_model.objects.filter(selected_bill=bill).first()
        if analyzed is None:
            # No analysed data — skip; nothing meaningful to export.
            continue
        try:
            rows.extend(row_builder(bill, analyzed))
        except Exception as exc:
            logger.exception(
                "Failed to render report row for %s bill %s: %s",
                filename_stub, bill.id, exc,
            )

    wb = build_report_workbook(rows, sheet_title=sheet_title)
    timestamp = tz_now().strftime("%Y%m%d-%H%M")
    filename = f"{filename_stub}-{timestamp}.xlsx"
    return workbook_to_response(wb, filename)


@extend_schema(summary="Download Tally Purchase Voucher report (xlsx)", tags=["Tally · Reports"])
@api_view(["GET"])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def tally_vendor_bills_report(request, org_id):
    return _build_and_stream(
        request,
        org_id,
        bill_model=TallyVendorBill,
        analyzed_model=TallyVendorAnalyzedBill,
        analyzed_bill_related_name="analysed_headers",
        row_builder=rows_from_vendor_analyzed,
        filename_stub="tally-purchase-vouchers",
        sheet_title="Purchase Vouchers",
    )


@extend_schema(summary="Download Tally Journal Entry report (xlsx)", tags=["Tally · Reports"])
@api_view(["GET"])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def tally_expense_bills_report(request, org_id):
    return _build_and_stream(
        request,
        org_id,
        bill_model=TallyExpenseBill,
        analyzed_model=TallyExpenseAnalyzedBill,
        analyzed_bill_related_name="analysed_headers",
        row_builder=rows_from_expense_analyzed,
        filename_stub="tally-journal-entries",
        sheet_title="Journal Entries",
    )


@extend_schema(summary="Download Tally Payment Voucher report (xlsx)", tags=["Tally · Reports"])
@api_view(["GET"])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def tally_payment_bills_report(request, org_id):
    return _build_and_stream(
        request,
        org_id,
        bill_model=TallyPaymentBill,
        analyzed_model=TallyPaymentAnalyzedBill,
        analyzed_bill_related_name="analysed_headers",
        row_builder=rows_from_expense_analyzed,
        filename_stub="tally-payment-vouchers",
        sheet_title="Payment Vouchers",
    )

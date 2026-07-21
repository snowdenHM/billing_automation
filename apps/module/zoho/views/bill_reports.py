"""
XLSX report endpoints for Zoho bills — Vendor, Journal, Expense.

Same format as the Tally reports (see
``apps.common.services.reports.REPORT_COLUMNS``). One endpoint per bill
type; each accepts an optional ``?status=Analysed|Verified|Synced``
filter (default: all three).
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
    ExpenseBill,
    ExpenseZohoBill,
    JournalBill,
    JournalZohoBill,
    VendorBill,
    VendorZohoBill,
)

logger = logging.getLogger(__name__)


_ALLOWED_STATUSES = {"Analysed", "Verified", "Synced"}
_DEFAULT_STATUSES = ["Analysed", "Verified", "Synced"]


def _resolve_statuses(request) -> list[str]:
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
    row_builder,
    filename_stub,
    sheet_title,
):
    """Zoho analysed bills link via ``selectBill`` (camelCase) — no reverse
    manager to lean on, so we always issue an explicit ``.filter``.
    """
    organization = get_organization_from_request(request, org_id)
    if not organization:
        return _org_not_found_response(org_id)

    statuses = _resolve_statuses(request)
    bills = (
        bill_model.objects.filter(organization=organization, status__in=statuses)
        .order_by("-created_at")
    )

    rows = []
    for bill in bills.iterator():
        analyzed = analyzed_model.objects.filter(selectBill=bill).order_by("-id").first()
        if analyzed is None:
            continue
        try:
            rows.extend(row_builder(bill, analyzed))
        except Exception as exc:
            logger.exception(
                "Failed to render report row for Zoho %s bill %s: %s",
                filename_stub, bill.id, exc,
            )

    wb = build_report_workbook(rows, sheet_title=sheet_title)
    timestamp = tz_now().strftime("%Y%m%d-%H%M")
    filename = f"{filename_stub}-{timestamp}.xlsx"
    return workbook_to_response(wb, filename)


@extend_schema(summary="Download Zoho Vendor Bill report (xlsx)", tags=["Zoho · Reports"])
@api_view(["GET"])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def zoho_vendor_bills_report(request, org_id):
    return _build_and_stream(
        request,
        org_id,
        bill_model=VendorBill,
        analyzed_model=VendorZohoBill,
        row_builder=rows_from_vendor_analyzed,
        filename_stub="zoho-vendor-bills",
        sheet_title="Vendor Bills",
    )


@extend_schema(summary="Download Zoho Journal Entry report (xlsx)", tags=["Zoho · Reports"])
@api_view(["GET"])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def zoho_journal_bills_report(request, org_id):
    return _build_and_stream(
        request,
        org_id,
        bill_model=JournalBill,
        analyzed_model=JournalZohoBill,
        row_builder=rows_from_expense_analyzed,
        filename_stub="zoho-journal-entries",
        sheet_title="Journal Entries",
    )


@extend_schema(summary="Download Zoho Expense Bill report (xlsx)", tags=["Zoho · Reports"])
@api_view(["GET"])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def zoho_expense_bills_report(request, org_id):
    return _build_and_stream(
        request,
        org_id,
        bill_model=ExpenseBill,
        analyzed_model=ExpenseZohoBill,
        row_builder=rows_from_expense_analyzed,
        filename_stub="zoho-expense-bills",
        sheet_title="Expense Bills",
    )

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

from apps.common import roles
from apps.common.permissions import HasOrgPermission, requires

from apps.common.services.reports import (
    build_report_workbook,
    rows_from_expense_analyzed,
    rows_from_vendor_analyzed,
    workbook_to_response,
)
from apps.common.bill_filters import resolve_report_ids
from apps.common.utils import get_organization_from_request

from ..models import (
    ExpenseBill,
    ExpenseZohoBill,
    JournalBill,
    JournalZohoBill,
    PaymentBill,
    PaymentZohoBill,
    ReceiptBill,
    ReceiptZohoBill,
    SalesBill,
    SalesZohoBill,
    VendorBill,
    VendorZohoBill,
)


class _PartyAsVendor:
    """Expose an analysed header's customer under the name the shared row
    builders read.

    ``rows_from_*_analyzed`` take the party off ``.vendor`` because they
    were written for the purchase side. On the sales/receipt side the
    party is a customer; wrapping is cheaper and less fragile than
    forking the builders or giving the models a misleading alias.
    """

    __slots__ = ("_analysed",)

    def __init__(self, analysed):
        self._analysed = analysed

    def __getattr__(self, name):
        if name == "vendor":
            return self._analysed.customer
        return getattr(self._analysed, name)


def _rows_with_customer_party(row_builder):
    def _build(bill, analysed):
        return row_builder(bill, _PartyAsVendor(analysed))
    return _build

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

    # `.alive()` — a trashed bill must not turn up in an export either.
    bills = bill_model.objects.alive().filter(organization=organization)

    # An explicit selection wins over the status filter: the user ticked
    # those exact rows, so export those exact rows.
    selected_ids = resolve_report_ids(request)
    if selected_ids is None:
        bills = bills.filter(status__in=_resolve_statuses(request))
    elif not selected_ids:
        return Response(
            {
                "error": "Nothing selected",
                "message": "Select at least one bill to export.",
                "error_code": "NO_BILLS_SELECTED",
            },
            status=400,
        )
    else:
        bills = bills.filter(id__in=selected_ids)

    bills = bills.order_by("-created_at")

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

    # A selection that yielded nothing means every chosen bill is still
    # un-analysed. Say so instead of handing back an empty spreadsheet.
    if selected_ids and not rows:
        return Response(
            {
                "error": "Nothing to export",
                "message": (
                    "None of the selected bills have analysed data yet. "
                    "Analyse them first, then download."
                ),
                "error_code": "NO_ANALYSED_DATA",
            },
            status=400,
        )

    wb = build_report_workbook(rows, sheet_title=sheet_title)
    timestamp = tz_now().strftime("%Y%m%d-%H%M")
    filename = f"{filename_stub}-{timestamp}.xlsx"
    return workbook_to_response(wb, filename)


@extend_schema(summary="Download Zoho Vendor Bill report (xlsx)", tags=["Zoho · Reports"])
@requires(roles.EXPORT_REPORTS)
@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated, HasOrgPermission])
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
@requires(roles.EXPORT_REPORTS)
@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated, HasOrgPermission])
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
@requires(roles.EXPORT_REPORTS)
@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated, HasOrgPermission])
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


@extend_schema(summary="Download Zoho Sales Invoice report (xlsx)", tags=["Zoho · Reports"])
@requires(roles.EXPORT_REPORTS)
@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated, HasOrgPermission])
def zoho_sales_bills_report(request, org_id):
    return _build_and_stream(
        request,
        org_id,
        bill_model=SalesBill,
        analyzed_model=SalesZohoBill,
        row_builder=_rows_with_customer_party(rows_from_vendor_analyzed),
        filename_stub="zoho-sales-invoices",
        sheet_title="Sales Invoices",
    )


@extend_schema(summary="Download Zoho Receipt Voucher report (xlsx)", tags=["Zoho · Reports"])
@requires(roles.EXPORT_REPORTS)
@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated, HasOrgPermission])
def zoho_receipt_bills_report(request, org_id):
    return _build_and_stream(
        request,
        org_id,
        bill_model=ReceiptBill,
        analyzed_model=ReceiptZohoBill,
        # No line items on a payment object, so the expense-shaped builder
        # emits the single bill-level row, which is all there is to say.
        row_builder=_rows_with_customer_party(rows_from_expense_analyzed),
        filename_stub="zoho-receipt-vouchers",
        sheet_title="Receipt Vouchers",
    )


@extend_schema(summary="Download Zoho Payment Voucher report (xlsx)", tags=["Zoho · Reports"])
@requires(roles.EXPORT_REPORTS)
@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated, HasOrgPermission])
def zoho_payment_bills_report(request, org_id):
    return _build_and_stream(
        request,
        org_id,
        bill_model=PaymentBill,
        analyzed_model=PaymentZohoBill,
        row_builder=rows_from_expense_analyzed,
        filename_stub="zoho-payment-vouchers",
        sheet_title="Payment Vouchers",
    )

"""Receipt vouchers for Zoho Books — money received from a customer.

Posts to Zoho's ``customerpayments``, not to a bill-shaped object: a
receipt has a party, an amount, a date, a mode and a deposit-to account,
and no product grid. Everything it shares with the payment voucher lives
in ``payment_voucher_helpers``; what differs:

  * **The party is the customer who paid**, taken from ``to`` — the
    receipt was issued by the organisation, so the payer is the
    recipient half of the document.

  * **The bank/cash account is the one the money arrived into**, posted
    as Zoho's ``account_id``. It is left unset by analysis on purpose:
    which account was used is not printed on the receipt, and a plausible
    guess in that field is worse than a blank one.
"""

import logging

from drf_spectacular.utils import extend_schema
from rest_framework.decorators import api_view, permission_classes, parser_classes
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.permissions import IsAuthenticated

from apps.common import roles
from apps.common.permissions import HasOrgPermission, requires

from apps.common.services.bill_analysis import analyze_bill_bytes, get_receipt_bill_prompt
from apps.common.services.duplicate_detection import check_duplicate_bill as _check_duplicate_bill_generic
from apps.common.services.pdf_processing import split_pdf_to_bills

from ..models import ReceiptBill, ReceiptZohoBill
from ..serializers.common import AnalysisResponseSerializer
from ..serializers.payment_bills import (
    ReceiptZohoBillSerializer,
    ZohoReceiptBillSerializer,
    ZohoReceiptBillDetailSerializer,
    ZohoReceiptBillMultipleUploadSerializer,
)
from .bill_view_helpers import (
    zoho_bills_list_base,
    zoho_bills_upload_base,
    zoho_bill_detail_base,
    zoho_bill_analyze_base,
    zoho_bill_delete_base,
)
from .helpers import find_zoho_customer_ledger_enhanced
from .payment_voucher_helpers import (
    ZOHO_CUSTOMER_PAYMENTS_URL,
    build_customer_payment_payload,
    zoho_payment_sync_base,
    zoho_payment_verify_base,
)

logger = logging.getLogger(__name__)

_ANALYSIS_FALLBACK = {
    "invoiceNumber": "", "dateIssued": "", "dueDate": "",
    "from": {"name": "", "address": "", "gst_number": ""},
    "to": {"name": "", "address": "", "gst_number": ""},
    "expenses": [], "total": 0, "igst": 0, "cgst": 0, "sgst": 0, "tds": 0,
}


# ============================================================================
# Thin helper wrappers
# ============================================================================

def check_duplicate_receipt_bill(bill, organization):
    return _check_duplicate_bill_generic(bill, organization, ReceiptBill)


def process_pdf_splitting_receipt(pdf_file, organization, file_type, uploaded_by):
    return split_pdf_to_bills(
        pdf_file, organization, file_type, uploaded_by,
        bill_model=ReceiptBill, filename_prefix="BM-Receipt-Page",
    )


def analyze_receipt_bill_with_openai(file_content, file_extension):
    """Analyse a receipt. The receipt prompt tells the model the letterhead
    issued the receipt and the payer is under 'Received From'."""
    logger.info("Starting receipt analysis for file type: %s", file_extension)
    try:
        return analyze_bill_bytes(file_content, file_extension, get_receipt_bill_prompt())
    except Exception as e:
        logger.error("Receipt analysis failed: %s", e)
        return {**_ANALYSIS_FALLBACK, "error": f"Analysis failed: {e}"}


def create_receipt_zoho_objects_from_analysis(bill, analyzed_data, organization):
    from .bill_creation import create_zoho_payment_objects_from_analysis

    return create_zoho_payment_objects_from_analysis(
        bill, analyzed_data, organization,
        analysed_model=ReceiptZohoBill,
        party_field='customer',
        party_source='to',
        party_lookup_fn=find_zoho_customer_ledger_enhanced,
        label='receipt',
    )


# ============================================================================
# Views
# ============================================================================

@extend_schema(responses=ZohoReceiptBillSerializer(many=True), tags=["Zoho Receipt Vouchers"], methods=["GET"])
@requires(roles.VIEW_BILLS)
@api_view(['GET'])
@permission_classes([IsAuthenticated, HasOrgPermission])
def receipt_bills_list_view(request, org_id):
    """List all receipt vouchers for the organization with pagination."""
    return zoho_bills_list_base(
        request, org_id, bill_model=ReceiptBill, list_serializer=ZohoReceiptBillSerializer,
    )


@extend_schema(
    summary="Upload Receipt Vouchers",
    request=ZohoReceiptBillMultipleUploadSerializer,
    responses={201: ZohoReceiptBillSerializer(many=True)},
    tags=["Zoho Receipt Vouchers"],
    methods=["POST"],
)
@requires(roles.UPLOAD_BILLS)
@api_view(['POST'])
@permission_classes([IsAuthenticated, HasOrgPermission])
@parser_classes([MultiPartParser, FormParser])
def receipt_bill_upload_view(request, org_id):
    """Handle single or multiple receipt uploads with PDF splitting."""
    from ..tasks import enqueue_receipt_bill_analysis, split_pdf_bill_receipt

    return zoho_bills_upload_base(
        request, org_id,
        bill_model=ReceiptBill,
        list_serializer=ZohoReceiptBillSerializer,
        upload_serializer=ZohoReceiptBillMultipleUploadSerializer,
        label='Receipt',
        check_duplicate_fn=check_duplicate_receipt_bill,
        split_pdf_fn=process_pdf_splitting_receipt,
        analyze_fn=analyze_receipt_bill_with_openai,
        create_objects_fn=create_receipt_zoho_objects_from_analysis,
        enqueue_analysis_fn=enqueue_receipt_bill_analysis,
        pdf_split_task_fn=split_pdf_bill_receipt,
    )


@extend_schema(responses=ZohoReceiptBillDetailSerializer, tags=["Zoho Receipt Vouchers"], methods=["GET"])
@requires(roles.VIEW_BILLS)
@api_view(['GET'])
@permission_classes([IsAuthenticated, HasOrgPermission])
def receipt_bill_detail_view(request, org_id, bill_id):
    """Get receipt voucher details including analysis data."""
    return zoho_bill_detail_base(
        request, org_id, bill_id,
        bill_model=ReceiptBill,
        zoho_bill_model=ReceiptZohoBill,
        detail_serializer=ZohoReceiptBillDetailSerializer,
        label='Receipt',
        select_related=['selectBill', 'customer', 'deposit_to_account'],
    )


@extend_schema(responses=AnalysisResponseSerializer, tags=["Zoho Receipt Vouchers"], methods=["POST"])
@requires(roles.ANALYSE_BILLS)
@api_view(['POST'])
@permission_classes([IsAuthenticated, HasOrgPermission])
def receipt_bill_analyze_view(request, org_id, bill_id):
    """Analyze a receipt using AI. Changes status from 'Draft' to 'Analysed'."""
    return zoho_bill_analyze_base(
        request, org_id, bill_id,
        bill_model=ReceiptBill,
        label='Receipt',
        check_duplicate_fn=check_duplicate_receipt_bill,
        analyze_fn=analyze_receipt_bill_with_openai,
        create_objects_fn=create_receipt_zoho_objects_from_analysis,
    )


@extend_schema(
    request=ReceiptZohoBillSerializer,
    responses=ReceiptZohoBillSerializer,
    tags=["Zoho Receipt Vouchers"],
    methods=["POST"],
)
@requires(roles.VERIFY_BILLS)
@api_view(['POST'])
@permission_classes([IsAuthenticated, HasOrgPermission])
def receipt_bill_verify_view(request, org_id, bill_id):
    """Verify and update receipt voucher data after analysis."""
    return zoho_payment_verify_base(
        request, org_id, bill_id,
        bill_model=ReceiptBill,
        analysed_model=ReceiptZohoBill,
        serializer_class=ReceiptZohoBillSerializer,
        label='Receipt',
    )


@extend_schema(
    responses={"200": {"detail": "Receipt synced to Zoho successfully"}},
    tags=["Zoho Receipt Vouchers"],
    methods=["POST"],
)
@requires(roles.SYNC_BILLS)
@api_view(['POST'])
@permission_classes([IsAuthenticated, HasOrgPermission])
def receipt_bill_sync_view(request, org_id, bill_id):
    """Sync a verified receipt to Zoho Books as a customer payment."""
    return zoho_payment_sync_base(
        request, org_id, bill_id,
        bill_model=ReceiptBill,
        analysed_model=ReceiptZohoBill,
        build_payload_fn=build_customer_payment_payload,
        endpoint_url=ZOHO_CUSTOMER_PAYMENTS_URL,
        label='Receipt',
        tag='RECEIPT',
    )


@extend_schema(
    responses={"204": None},
    tags=["Zoho Receipt Vouchers"],
    methods=["DELETE"],
)
@requires(roles.TRASH_BILLS)
@api_view(['DELETE'])
@permission_classes([IsAuthenticated, HasOrgPermission])
def receipt_bill_delete_view(request, org_id, bill_id):
    """Delete a receipt voucher and its associated file."""
    return zoho_bill_delete_base(request, org_id, bill_id, bill_model=ReceiptBill, label='Receipt')

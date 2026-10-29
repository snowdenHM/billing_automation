"""Payment vouchers for Zoho Books — money paid to a vendor.

Posts to Zoho's ``vendorpayments``. Structural mirror of
``receipt_bills``; what differs:

  * **The party is the vendor who was paid**, taken from ``from`` — a
    payment advice is issued against a document the vendor sent, so the
    party is the issuer half.

  * **The bank/cash account is the one the money left from**, posted as
    Zoho's ``paid_through_account_id``. Left unset by analysis for the
    same reason the receipt side leaves its deposit account unset.

  * **No dedicated prompt.** Payment advices and expense bills are the
    same document shape to the extractor, so this reuses
    ``get_expense_bill_prompt`` exactly as the Tally payment feed does.
"""

import logging

from drf_spectacular.utils import extend_schema
from rest_framework.decorators import api_view, permission_classes, parser_classes
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.permissions import IsAuthenticated

from apps.common import roles
from apps.common.permissions import HasOrgPermission, requires

from apps.common.services.bill_analysis import analyze_bill_bytes, get_expense_bill_prompt
from apps.common.services.duplicate_detection import check_duplicate_bill as _check_duplicate_bill_generic
from apps.common.services.pdf_processing import split_pdf_to_bills

from ..models import PaymentBill, PaymentZohoBill
from ..serializers.common import AnalysisResponseSerializer
from ..serializers.payment_bills import (
    PaymentZohoBillSerializer,
    ZohoPaymentBillSerializer,
    ZohoPaymentBillDetailSerializer,
    ZohoPaymentBillMultipleUploadSerializer,
)
from .bill_view_helpers import (
    zoho_bills_list_base,
    zoho_bills_upload_base,
    zoho_bill_detail_base,
    zoho_bill_analyze_base,
    zoho_bill_delete_base,
)
from .helpers import find_zoho_vendor_ledger_enhanced
from .payment_voucher_helpers import (
    ZOHO_VENDOR_PAYMENTS_PATH,
    build_vendor_payment_payload,
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

def check_duplicate_payment_bill(bill, organization):
    return _check_duplicate_bill_generic(bill, organization, PaymentBill)


def process_pdf_splitting_payment(pdf_file, organization, file_type, uploaded_by):
    return split_pdf_to_bills(
        pdf_file, organization, file_type, uploaded_by,
        bill_model=PaymentBill, filename_prefix="BM-Payment-Page",
    )


def analyze_payment_bill_with_openai(file_content, file_extension):
    """Analyse a payment advice. Reuses the expense prompt — to the
    extractor the two documents carry the same fields."""
    logger.info("Starting payment analysis for file type: %s", file_extension)
    try:
        return analyze_bill_bytes(file_content, file_extension, get_expense_bill_prompt())
    except Exception as e:
        logger.error("Payment analysis failed: %s", e)
        return {**_ANALYSIS_FALLBACK, "error": f"Analysis failed: {e}"}


def create_payment_zoho_objects_from_analysis(bill, analyzed_data, organization):
    from .bill_creation import create_zoho_payment_objects_from_analysis

    return create_zoho_payment_objects_from_analysis(
        bill, analyzed_data, organization,
        analysed_model=PaymentZohoBill,
        party_field='vendor',
        party_source='from',
        party_lookup_fn=find_zoho_vendor_ledger_enhanced,
        label='payment',
    )


# ============================================================================
# Views
# ============================================================================

@extend_schema(responses=ZohoPaymentBillSerializer(many=True), tags=["Zoho Payment Vouchers"], methods=["GET"])
@requires(roles.VIEW_BILLS)
@api_view(['GET'])
@permission_classes([IsAuthenticated, HasOrgPermission])
def payment_bills_list_view(request, org_id):
    """List all payment vouchers for the organization with pagination."""
    return zoho_bills_list_base(
        request, org_id, bill_model=PaymentBill, list_serializer=ZohoPaymentBillSerializer,
    )


@extend_schema(
    summary="Upload Payment Vouchers",
    request=ZohoPaymentBillMultipleUploadSerializer,
    responses={201: ZohoPaymentBillSerializer(many=True)},
    tags=["Zoho Payment Vouchers"],
    methods=["POST"],
)
@requires(roles.UPLOAD_BILLS)
@api_view(['POST'])
@permission_classes([IsAuthenticated, HasOrgPermission])
@parser_classes([MultiPartParser, FormParser])
def payment_bill_upload_view(request, org_id):
    """Handle single or multiple payment uploads with PDF splitting."""
    from ..tasks import enqueue_payment_bill_analysis, split_pdf_bill_payment

    return zoho_bills_upload_base(
        request, org_id,
        bill_model=PaymentBill,
        list_serializer=ZohoPaymentBillSerializer,
        upload_serializer=ZohoPaymentBillMultipleUploadSerializer,
        label='Payment',
        check_duplicate_fn=check_duplicate_payment_bill,
        split_pdf_fn=process_pdf_splitting_payment,
        analyze_fn=analyze_payment_bill_with_openai,
        create_objects_fn=create_payment_zoho_objects_from_analysis,
        enqueue_analysis_fn=enqueue_payment_bill_analysis,
        pdf_split_task_fn=split_pdf_bill_payment,
    )


@extend_schema(responses=ZohoPaymentBillDetailSerializer, tags=["Zoho Payment Vouchers"], methods=["GET"])
@requires(roles.VIEW_BILLS)
@api_view(['GET'])
@permission_classes([IsAuthenticated, HasOrgPermission])
def payment_bill_detail_view(request, org_id, bill_id):
    """Get payment voucher details including analysis data."""
    return zoho_bill_detail_base(
        request, org_id, bill_id,
        bill_model=PaymentBill,
        zoho_bill_model=PaymentZohoBill,
        detail_serializer=ZohoPaymentBillDetailSerializer,
        label='Payment',
        select_related=['selectBill', 'vendor', 'paid_through_account'],
    )


@extend_schema(responses=AnalysisResponseSerializer, tags=["Zoho Payment Vouchers"], methods=["POST"])
@requires(roles.ANALYSE_BILLS)
@api_view(['POST'])
@permission_classes([IsAuthenticated, HasOrgPermission])
def payment_bill_analyze_view(request, org_id, bill_id):
    """Analyze a payment using AI. Changes status from 'Draft' to 'Analysed'."""
    return zoho_bill_analyze_base(
        request, org_id, bill_id,
        bill_model=PaymentBill,
        label='Payment',
        check_duplicate_fn=check_duplicate_payment_bill,
        analyze_fn=analyze_payment_bill_with_openai,
        create_objects_fn=create_payment_zoho_objects_from_analysis,
    )


@extend_schema(
    request=PaymentZohoBillSerializer,
    responses=PaymentZohoBillSerializer,
    tags=["Zoho Payment Vouchers"],
    methods=["POST"],
)
@requires(roles.VERIFY_BILLS)
@api_view(['POST'])
@permission_classes([IsAuthenticated, HasOrgPermission])
def payment_bill_verify_view(request, org_id, bill_id):
    """Verify and update payment voucher data after analysis."""
    return zoho_payment_verify_base(
        request, org_id, bill_id,
        bill_model=PaymentBill,
        analysed_model=PaymentZohoBill,
        serializer_class=PaymentZohoBillSerializer,
        label='Payment',
    )


@extend_schema(
    responses={"200": {"detail": "Payment synced to Zoho successfully"}},
    tags=["Zoho Payment Vouchers"],
    methods=["POST"],
)
@requires(roles.SYNC_BILLS)
@api_view(['POST'])
@permission_classes([IsAuthenticated, HasOrgPermission])
def payment_bill_sync_view(request, org_id, bill_id):
    """Sync a verified payment to Zoho Books as a vendor payment."""
    return zoho_payment_sync_base(
        request, org_id, bill_id,
        bill_model=PaymentBill,
        analysed_model=PaymentZohoBill,
        build_payload_fn=build_vendor_payment_payload,
        endpoint_path=ZOHO_VENDOR_PAYMENTS_PATH,
        label='Payment',
        tag='PAYMENT',
    )


@extend_schema(
    responses={"204": None},
    tags=["Zoho Payment Vouchers"],
    methods=["DELETE"],
)
@requires(roles.TRASH_BILLS)
@api_view(['DELETE'])
@permission_classes([IsAuthenticated, HasOrgPermission])
def payment_bill_delete_view(request, org_id, bill_id):
    """Delete a payment voucher and its associated file."""
    return zoho_bill_delete_base(request, org_id, bill_id, bill_model=PaymentBill, label='Payment')

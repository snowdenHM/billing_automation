# apps/module/tally/views/payment_bills.py
#
# Structural clone of ``expense_bills.py`` for the Payment Voucher stack.
# Same request shape, same response shape — the only behavioural
# differences are:
#   * Payable/Paid ledger defaults to blank (user picks Bank/Cash).
#   * Sync payload emits ``"voucher_type": "Payment"`` instead of "Journal".
# Downstream Tally TCP/TDL handler must know how to route a Payment
# voucher; the XML shape and ledger structure are unchanged.

import json
import logging
import os
import random
from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO

from PyPDF2 import PdfReader
from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, OpenApiResponse
from pdf2image import convert_from_bytes
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, parser_classes
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.pagination import DefaultPagination
from apps.common.permissions import IsOrgAdmin, OrganizationAPIKeyOrBearerToken
from apps.common.utils import (
    get_organization_from_request, calculate_string_similarity,
    safe_get_nested, parse_bill_date, normalize_company_name, calculate_gst_rate,
    normalize_product_gst
)
from apps.common.converters import safe_decimal, to_decimal, to_int, safe_float_convert
from apps.common.services.duplicate_detection import check_duplicate_bill, update_bill_duplicate_metadata
from apps.common.services.pdf_processing import split_pdf_to_bills
from apps.common.services.bill_analysis import analyze_bill_file, get_expense_bill_prompt
from apps.organizations.models import Organization
from ..models import (
    TallyPaymentBill,
    TallyPaymentAnalyzedBill,
    TallyPaymentAnalyzedProduct,
    TallyPaymentConsolidatedProduct,
    TallyPaymentGstLine,
    Ledger,
    ParentLedger,
    TallyConfig,
    TallyVendorBill
)
from .vendor_bills import _sync_data_to_xml, _clean_tally_text
from ..serializers import (
    TallyPaymentBillSerializer,
    TallyPaymentAnalyzedBillSerializer,
    PaymentBillUploadSerializer,
    PaymentBillAnalysisRequestSerializer,
    PaymentBillVerificationSerializer,
    PaymentBillSyncRequestSerializer,
    PaymentBillSyncResponseSerializer
)

# OpenAI Client – no longer used directly; AI analysis is handled by common services
logger = logging.getLogger(__name__)


# ============================================================================
# Helper aliases (duplicates removed – now imported from apps.common)
# ============================================================================

# Backward-compatible aliases for local call-sites
_to_decimal = to_decimal
_to_int = to_int
safe_int_convert = to_int
parse_payment_bill_date = parse_bill_date


# Sentinel strings the FE historically sent when a ledger slot was left blank.
# Backend guard is case-insensitive — a "no tax ledger" round-trip must not
# spawn a bogus ledger on re-verify.
_BLANK_LEDGER_SENTINELS = frozenset({
    "", "no tax ledger", "no coa ledger", "no purchase ledger", "none", "null",
})


def _is_blank_payment_ledger(value):
    return not value or str(value).strip().lower() in _BLANK_LEDGER_SENTINELS


def _resolve_payment_tax_ledger(entry, tax_type, organization):
    """UUID-first ledger resolve for Payment vouchers.

    Order:
      1. explicit ``ledger_id`` UUID (FE dropdown value)
      2. ``ledger`` string that parses as an existing UUID
      3. ``ledger`` string treated as a name (case-insensitive sentinel skip)

    Returns None when the slot should be cleared (blank sentinel) or when
    the identifier does not resolve.
    """
    if not isinstance(entry, dict):
        return None
    ledger_id = entry.get("ledger_id")
    if ledger_id:
        try:
            return Ledger.objects.get(id=ledger_id, organization=organization)
        except (Ledger.DoesNotExist, ValueError, DjangoValidationError):
            pass
    raw = entry.get("ledger")
    if _is_blank_payment_ledger(raw):
        return None
    # Try UUID first if the string parses.
    try:
        return Ledger.objects.get(id=str(raw).strip(), organization=organization)
    except (Ledger.DoesNotExist, ValueError, DjangoValidationError):
        pass
    return find_or_create_payment_tax_ledger(str(raw).strip(), tax_type, organization)


def check_duplicate_tally_payment_bill(bill, organization):
    """Wrapper: delegates to apps.common.services.duplicate_detection.check_duplicate_bill."""
    return check_duplicate_bill(bill, organization, TallyPaymentBill)


# ---------------------------------------------------------------------------
# AI payload readers
#
# Payment vouchers are analysed with ``get_expense_bill_prompt`` (see
# ``analyze_payment_bill_with_ai``), so the model returns line items under
# ``expenses`` and the invoice number under ``invoiceNumber`` — the same
# shape ``expense_bills.py`` consumes.
#
# This module used to read ``payments`` and ``billNumber``, keys the prompt
# never emits. The result was silent: analysis "succeeded", the header
# amounts came through, but every voucher landed with zero line items and an
# empty ``bill_no``. Both readers below accept the legacy spellings too, so
# anything already stored under the old keys still loads.
# ---------------------------------------------------------------------------

def extract_payment_line_items(data):
    """Return the AI line items, whichever key the payload carries."""
    if not isinstance(data, dict):
        return []
    for key in ("expenses", "payments", "items"):
        value = data.get(key)
        if isinstance(value, list) and value:
            return value
    return []


def extract_payment_bill_number(data):
    """Return the invoice number, tolerating both key spellings."""
    if not isinstance(data, dict):
        return ""
    for key in ("invoiceNumber", "billNumber"):
        value = data.get(key)
        if value:
            return str(value).strip()
    return ""


def analyze_payment_bill_with_ai(bill, organization):
    """Analyze payment bill using OpenAI API — delegates to shared service."""
    logger.info(f"Starting AI analysis for payment bill {bill.id}, file: {bill.file.name}")

    try:
        json_data = analyze_bill_file(bill.file.path, bill.file.name, get_expense_bill_prompt())
        logger.info(f"AI analysis successful for payment bill {bill.id}")
    except Exception as e:
        logger.error(f"AI analysis failed for payment bill {bill.id}: {e}")
        raise Exception(f"AI processing failed: {str(e)}")

    # Process and save extracted data
    return process_payment_analysis_data(bill, json_data, organization)


def validate_payment_bill_ownership(json_data, organization):
    """Validate if the payment bill belongs to the organization — delegates to shared service.

    Checks the ``to`` field (customer/recipient). Lenient when no customer info
    is present (common for general receipts).
    """
    from apps.common.services.ownership import validate_bill_ownership_simple
    return validate_bill_ownership_simple(
        json_data, organization, check_field='to', allow_empty=True,
        bill_type='payment bill',
    )


def process_payment_analysis_data(bill, json_data, organization):
    """Process AI extracted data and create analyzed payment bill with automatic vendor and tax selection"""
    try:
        # 🛡️ STEP 1: Validate bill ownership
        ownership_valid, ownership_message = validate_payment_bill_ownership(json_data, organization)
        if not ownership_valid:
            logger.warning(f"⚠️ Payment bill {bill.id} ownership validation failed: {ownership_message}")
            # Store ownership validation results only if fields exist
            if hasattr(bill.__class__, 'ownership_validation_status'):
                bill.ownership_validation_status = 'failed'
            if hasattr(bill.__class__, 'ownership_validation_message'):
                bill.ownership_validation_message = ownership_message
        else:
            logger.info(f"✅ Payment bill {bill.id} ownership validation passed: {ownership_message}")
            # Store ownership validation results only if fields exist
            if hasattr(bill.__class__, 'ownership_validation_status'):
                bill.ownership_validation_status = 'passed'
            if hasattr(bill.__class__, 'ownership_validation_message'):
                bill.ownership_validation_message = ownership_message
            
        logger.info(f"Processing payment analysis data for bill {bill.id} with automation")
            
        # Log the raw JSON data for debugging
        logger.info(f"Raw JSON data from OpenAI: {json.dumps(json_data, indent=2)}")

        # Extract relevant data with robust error handling
        relevant_data = {}

        # Handle different JSON response formats from OpenAI
        if isinstance(json_data, dict):
            if "properties" in json_data:
                # Handle schema format - extract from properties with safe access
                try:
                    # Key names mirror the prompt exactly, so this branch and
                    # the direct branch below store an identically-shaped
                    # ``analysed_data``. Duplicate detection reads
                    # ``invoiceNumber``, and the frontend falls back to it
                    # too — the old ``billNumber`` spelling matched neither.
                    relevant_data = {
                        "invoiceNumber": safe_get_nested(json_data, ["properties", "invoiceNumber", "const"], ""),
                        "dateIssued": safe_get_nested(json_data, ["properties", "dateIssued", "const"], ""),
                        "dueDate": safe_get_nested(json_data, ["properties", "dueDate", "const"], ""),
                        "from": safe_get_nested(json_data, ["properties", "from", "properties"], {}),
                        "to": safe_get_nested(json_data, ["properties", "to", "properties"], {}),
                        "expenses": extract_payments_from_properties(json_data),
                        "total": safe_get_nested(json_data, ["properties", "total", "const"], 0),
                        "igst": safe_get_nested(json_data, ["properties", "igst", "const"], 0),
                        "cgst": safe_get_nested(json_data, ["properties", "cgst", "const"], 0),
                        "sgst": safe_get_nested(json_data, ["properties", "sgst", "const"], 0),
                        "tds": safe_get_nested(json_data, ["properties", "tds", "const"], 0),
                    }
                except Exception as e:
                    logger.warning(f"Failed to extract from properties format, trying direct access: {e}")
                    relevant_data = json_data
            else:
                # Direct format - use the data as is
                relevant_data = json_data
        else:
            logger.warning(f"Unexpected JSON data type: {type(json_data)}")
            raise Exception("Invalid JSON data format from OpenAI")

        # OCR sanity check — flags leading-digit misses on 7-8 digit
        # amounts (Corrections 14 + 24).
        try:
            from apps.common.services.bill_analysis import check_ocr_totals_sanity
            _sanity = check_ocr_totals_sanity(relevant_data)
            relevant_data['_ocr_sanity'] = _sanity
            if not _sanity.get('ok'):
                logger.warning(
                    "OCR sanity failed for payment bill %s: %s",
                    bill.id, _sanity.get('message'),
                )
        except Exception as _san_err:
            logger.warning("OCR sanity check errored on bill %s: %s", bill.id, _san_err)

        # Save analyzed data to bill
        bill.analysed_data = relevant_data
        bill.save(update_fields=['analysed_data'])

        # Extract required fields with safe access
        bill_number = extract_payment_bill_number(relevant_data)
        date_issued = str(relevant_data.get('dateIssued', ''))

        # Handle 'from' field safely with GST number extraction
        from_data = relevant_data.get('from', {})
        if isinstance(from_data, dict):
            company_name = str(from_data.get('name', '')).strip()
            vendor_gst_number = str(from_data.get('gst_number', '')).strip()
        else:
            company_name = str(from_data).strip()
            vendor_gst_number = ''

        # Parse date with multiple format support
        bill_date = parse_payment_bill_date(date_issued)
        
        # Parse due date if provided  
        due_date_issued = str(relevant_data.get('dueDate', ''))
        due_date = parse_payment_bill_date(due_date_issued) if due_date_issued else None

        # Extract financial information with GST details
        igst_val = _to_decimal(relevant_data.get('igst', 0))
        cgst_val = _to_decimal(relevant_data.get('cgst', 0))  
        sgst_val = _to_decimal(relevant_data.get('sgst', 0))
        tds_val = _to_decimal(relevant_data.get('tds', 0))
        total_val = _to_decimal(relevant_data.get('total', 0))

        logger.info(f"Extracted payment data - Bill: {bill_number}, Vendor: {company_name}, GST: {vendor_gst_number}, Total: ₹{total_val}, IGST: {igst_val}, CGST: {cgst_val}, SGST: {sgst_val}")
        
        # 🎯 STEP 2: Payment vouchers leave the Payable/Paid slot BLANK
        # by design (product spec) — the user must manually pick a
        # Bank/Cash ledger. Auto-selecting a vendor ledger here would
        # defeat that flow, so we deliberately skip the lookup.
        vendor_ledger = None
        logger.info(
            "Payment voucher: leaving Payable/Paid ledger blank for user "
            "selection (parsed vendor: %s)", company_name,
        )

        # Determine GST type with safe conversion and proper decimal rounding
        if igst_val > 0:
            gst_type = TallyPaymentAnalyzedBill.GSTType.IGST
        elif cgst_val > 0 or sgst_val > 0:
            gst_type = TallyPaymentAnalyzedBill.GSTType.CGST_SGST
        else:
            gst_type = TallyPaymentAnalyzedBill.GSTType.UNKNOWN

        # 🎯 STEP 3: Automatic tax ledger finding for bill-level GST values
        logger.info(f"[NEW EXPENSE] Finding bill-level tax ledgers - IGST: {igst_val}, CGST: {cgst_val}, SGST: {sgst_val}")
        
        igst_tax_ledger = find_appropriate_payment_tax_ledger(organization, float(igst_val), 0, 0, ledger_type='bill') if igst_val > 0 else None
        cgst_tax_ledger = find_appropriate_payment_tax_ledger(organization, 0, float(cgst_val), 0, ledger_type='bill') if cgst_val > 0 else None
        sgst_tax_ledger = find_appropriate_payment_tax_ledger(organization, 0, 0, float(sgst_val), ledger_type='bill') if sgst_val > 0 else None

        if igst_tax_ledger:
            logger.info(f"✅ Automatically selected IGST ledger: {igst_tax_ledger.name} (Amount: ₹{igst_val})")
        if cgst_tax_ledger:
            logger.info(f"✅ Automatically selected CGST ledger: {cgst_tax_ledger.name} (Amount: ₹{cgst_val})")
        if sgst_tax_ledger:
            logger.info(f"✅ Automatically selected SGST ledger: {sgst_tax_ledger.name} (Amount: ₹{sgst_val})")

        # 🎯 STEP 4: Find payment COA ledger for line items
        payment_coa_ledger = find_appropriate_payment_tax_ledger(organization, 0, 0, 0, ledger_type='payment_coa')
        if payment_coa_ledger:
            logger.info(f"✅ Automatically selected Payment COA ledger: {payment_coa_ledger.name}")

        # Create analyzed bill with automatic selections.
        # ``get_or_create`` is idempotent under the double-analysis race
        # (see #4 audit) — if a row already exists we update the header
        # fields with the newer values (last-write-wins).
        with transaction.atomic():
            analyzed_bill, _created = TallyPaymentAnalyzedBill.objects.get_or_create(
                selected_bill=bill,
                defaults={
                    "vendor": vendor_ledger,
                    "bill_no": bill_number,
                    "bill_date": bill_date,
                    "due_date": due_date,
                    "igst": igst_val,
                    "cgst": cgst_val,
                    "sgst": sgst_val,
                    "tds": tds_val,
                    "total": total_val,
                    "igst_taxes": igst_tax_ledger,
                    "cgst_taxes": cgst_tax_ledger,
                    "sgst_taxes": sgst_tax_ledger,
                    "note": "AI Analyzed Payment Bill with Automation",
                    "organization": organization,
                    "gst_type": gst_type,
                    "igst_debit_or_credit": "debit",
                    "cgst_debit_or_credit": "debit",
                    "sgst_debit_or_credit": "debit",
                    "tds_debit_or_credit": "credit",
                },
            )
            if not _created:
                analyzed_bill.vendor = vendor_ledger
                analyzed_bill.bill_no = bill_number
                analyzed_bill.bill_date = bill_date
                analyzed_bill.due_date = due_date
                analyzed_bill.igst = igst_val
                analyzed_bill.cgst = cgst_val
                analyzed_bill.sgst = sgst_val
                analyzed_bill.tds = tds_val
                analyzed_bill.total = total_val
                analyzed_bill.igst_taxes = igst_tax_ledger
                analyzed_bill.cgst_taxes = cgst_tax_ledger
                analyzed_bill.sgst_taxes = sgst_tax_ledger
                analyzed_bill.gst_type = gst_type
                analyzed_bill.save()

            logger.info(f"✅ Created TallyPaymentAnalyzedBill with ID: {analyzed_bill.id} (with automatic vendor/tax selections)")

            # Create analyzed products (payment items) with auto-assigned COA ledger
            product_instances = []
            payments = extract_payment_line_items(relevant_data)
            if not payments:
                logger.warning(
                    "Payment bill %s: AI returned no line items (analysed_data keys: %s)",
                    bill.id, sorted(relevant_data.keys()) if isinstance(relevant_data, dict) else type(relevant_data),
                )
            if isinstance(payments, list):
                for payment in payments:
                    if isinstance(payment, dict):
                        # Find specific COA ledger for this payment category
                        payment_category = str(payment.get('category', 'General Payments'))
                        item_coa_ledger = find_or_create_payment_chart_of_accounts_ledger(payment_category, organization)
                        
                        product = TallyPaymentAnalyzedProduct(
                            payment_bill=analyzed_bill,
                            item_details=str(payment.get('description', '')),
                            chart_of_accounts=item_coa_ledger or payment_coa_ledger,  # Use specific or fallback COA ledger
                            amount=_to_decimal(payment.get('amount', 0)),
                            debit_or_credit=TallyPaymentAnalyzedProduct.DebitCredit.DEBIT,
                            organization=organization
                        )
                        product_instances.append(product)
                        
                        if item_coa_ledger:
                            logger.info(f"✅ Automatically selected COA ledger for '{payment_category}': {item_coa_ledger.name}")

            if product_instances:
                TallyPaymentAnalyzedProduct.objects.bulk_create(product_instances)
                logger.info(f"Successfully created {len(product_instances)} payment products with automatic COA assignments")

                # ✅ CREATE CONSOLIDATED PRODUCTS FOR LAYOUT SWITCHING SUPPORT
                if len(product_instances) > 1:
                    try:
                        total_amount = sum(p.amount for p in product_instances)
                        items_count = len(product_instances)

                        item_details = []
                        for product in product_instances:
                            item_details.append(f'• {product.item_details} (Amount: ₹{product.amount})')

                        consolidated_details = f'Consolidated {items_count} payment entries:\n' + '\n'.join(item_details)

                        consolidated_product = TallyPaymentConsolidatedProduct.objects.create(
                            payment_bill=analyzed_bill,
                            organization=organization,
                            item_details=consolidated_details,
                            chart_of_accounts=payment_coa_ledger,  # Use fallback COA ledger for consolidated
                            amount=total_amount,
                            debit_or_credit=TallyPaymentConsolidatedProduct.DebitCredit.DEBIT,
                            original_entries_count=items_count,
                            consolidation_notes=f'Auto-created with automation for {items_count} payment entries'
                        )

                        logger.info(f"✅ Created consolidated payment product for bill {analyzed_bill.id} with {items_count} entries (₹{total_amount})")

                        # Initial analysis with 2+ items — flip to consolidate
                        # mode so the FE opens with the aggregated view (mirrors
                        # vendor/expense behaviour).
                        if not analyzed_bill.consolidate:
                            analyzed_bill.consolidate = True
                            analyzed_bill.save(update_fields=['consolidate'])

                    except Exception as e:
                        logger.error(f"❌ Error creating consolidated payment product for bill {analyzed_bill.id}: {str(e)}")
                else:
                    logger.info(f"ℹ️ Skipping consolidated payment product creation - bill has only {len(product_instances)} item(s)")

            # Update bill status and save ownership validation results
            bill.status = TallyPaymentBill.BillStatus.ANALYSED
            bill.process = True
            # Update fields list based on what exists in the model
            fields_to_update = ['status', 'process']
            if hasattr(bill, 'ownership_validation_status'):
                fields_to_update.append('ownership_validation_status')
            if hasattr(bill, 'ownership_validation_message'):
                fields_to_update.append('ownership_validation_message')
            bill.save(update_fields=fields_to_update)

            # Check for duplicates
            duplicate_result = check_duplicate_tally_payment_bill(bill, organization)
            if duplicate_result:
                is_duplicate, duplicate_bills, similarity_score = duplicate_result
                if is_duplicate:
                    logger.warning(f"⚠️ Potential duplicate payment bill detected with similarity {similarity_score}")
                    
            logger.info(f"✅ Successfully processed payment analysis for bill {bill.id} with full automation")

            return analyzed_bill

    except Exception as e:
        logger.error(f"❌ Error processing payment analysis data: {str(e)} - Data: {json_data}")
        # Update bill status to failed
        bill.status = TallyPaymentBill.BillStatus.FAILED
        fields_to_update = ['status']
        
        # Only update ownership validation fields if they exist in the model
        if hasattr(bill, 'ownership_validation_status'):
            bill.ownership_validation_status = 'error'
            fields_to_update.append('ownership_validation_status')
        
        if hasattr(bill, 'ownership_validation_message'):
            bill.ownership_validation_message = f"Processing failed: {str(e)}"
            fields_to_update.append('ownership_validation_message')
        
        bill.save(update_fields=fields_to_update)
        raise Exception(f"Error processing payment analysis data: {str(e)}")


def extract_payments_from_properties(json_data):
    """Safely extract line items from the schema-shaped ("properties") format.

    The prompt nests them under ``expenses``; ``payments`` is checked only
    so an older stored payload still parses.
    """
    try:
        payments_data = safe_get_nested(json_data, ["properties", "expenses", "items"], [])
        if not payments_data:
            payments_data = safe_get_nested(json_data, ["properties", "payments", "items"], [])
        if isinstance(payments_data, list):
            extracted_payments = []
            for payment in payments_data:
                if isinstance(payment, dict):
                    extracted_payment = {
                        "description": safe_get_nested(payment, ["description", "const"], ""),
                        "category": safe_get_nested(payment, ["category", "const"], ""),
                        "amount": safe_get_nested(payment, ["amount", "const"], 0)
                    }
                    extracted_payments.append(extracted_payment)
            return extracted_payments
        return []
    except Exception:
        return []


def find_appropriate_payment_tax_ledger(organization, igst_val, cgst_val, sgst_val, ledger_type='bill'):
    """Delegates to shared find_tally_tax_ledger in helpers."""
    from .helpers import find_tally_tax_ledger
    return find_tally_tax_ledger(organization, igst_val, cgst_val, sgst_val, ledger_type=ledger_type)


def find_payment_vendor_ledger(company_name, organization, vendor_gst=None):
    """Delegates to shared find_tally_vendor_ledger in helpers."""
    from .helpers import find_tally_vendor_ledger
    return find_tally_vendor_ledger(
        company_name, organization, vendor_gst=vendor_gst,
        use_config_threshold=True, gst_search_scope='all',
        fallback_all_ledgers=True
    )


def process_pdf_splitting_payment(pdf_file, organization, file_type, uploaded_by):
    """Split PDF into individual pages and create separate payment bills"""
    return split_pdf_to_bills(
        pdf_file=pdf_file,
        organization=organization,
        file_type=file_type,
        uploaded_by=uploaded_by,
        bill_model=TallyPaymentBill,
        filename_prefix="BM-Payment-Page"
    )


# ============================================================================
# API Views
# ✅
@extend_schema(
    summary="List Payment Bills",
    description="Get all payment bills for the organization",
    responses={200: TallyPaymentBillSerializer(many=True)},
    tags=['Tally Payment Bills']
)
@api_view(['GET'])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def payment_bills_list(request, org_id):
    """Get all payment bills for the organization."""
    from .bill_helpers import bills_list_base
    return bills_list_base(
        request, org_id,
        bill_model=TallyPaymentBill,
        serializer_class=TallyPaymentBillSerializer,
        include_ownership_filter=False,  # Payment doesn't have ownership filter
    )


# ✅
@extend_schema(
    summary="Upload Payment Bills",
    description="Upload single or multiple payment bill files (PDF, JPG, PNG). Supports both single file and multiple file uploads.",
    request=PaymentBillUploadSerializer,
    responses={201: TallyPaymentBillSerializer(many=True)},
    tags=['Tally Payment Bills']
)
@api_view(['POST'])
@permission_classes([IsAuthenticated, IsOrgAdmin])
@parser_classes([MultiPartParser, FormParser])
def payment_bills_upload(request, org_id):
    """Handle single or multiple payment bill file uploads with PDF splitting support"""
    from .bill_helpers import bills_upload_base
    from ..tasks import enqueue_payment_bill_processing, split_pdf_bill_payment

    return bills_upload_base(
        request=request,
        org_id=org_id,
        bill_model=TallyPaymentBill,
        upload_serializer_class=PaymentBillUploadSerializer,
        response_serializer_class=TallyPaymentBillSerializer,
        pdf_split_func=process_pdf_splitting_payment,
        enqueue_func=enqueue_payment_bill_processing,
        bill_type_label="payment",
        pdf_split_task_fn=split_pdf_bill_payment,
    )


# ✅
@extend_schema(
    summary="Check Bill Processing Status",
    description="Check the status of background processing for an payment bill",
    responses={200: "Bill processing status information"},
    tags=['Tally Payment Bills']
)
@api_view(['GET'])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def payment_bill_processing_status(request, org_id, bill_id):
    """Check the processing status of an payment bill"""
    from .bill_helpers import bill_processing_status_base
    return bill_processing_status_base(request, org_id, bill_id, TallyPaymentBill, "Payment")


# ✅
@extend_schema(
    summary="Analyze Payment Bill",
    description="Analyze payment bill using OpenAI to extract payment data",
    request=PaymentBillAnalysisRequestSerializer,
    responses={
        200: TallyPaymentAnalyzedBillSerializer,
        400: OpenApiResponse(description="Analysis failed")
    },
    tags=['Tally Payment Bills']
)
@api_view(['POST'])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def payment_bill_analyze(request, org_id):
    """Analyze payment bill using OpenAI"""
    serializer = PaymentBillAnalysisRequestSerializer(data=request.data)
    if not serializer.is_valid():
        return Response({
            'error': 'Invalid Analysis Request',
            'message': 'The payment bill analysis request data is invalid. Please ensure the bill_id is provided and valid.',
            'details': serializer.errors,
            'error_code': 'INVALID_ANALYSIS_REQUEST'
        }, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

    bill_id = serializer.validated_data['bill_id']
    organization = get_organization_from_request(request, org_id)

    try:
        bill = TallyPaymentBill.objects.alive().get(
            id=bill_id,
            organization=organization
        )
    except TallyPaymentBill.DoesNotExist:
        return Response(
            {'error': 'Bill not found'},
            status=status.HTTP_404_NOT_FOUND
        )

    if bill.process:
        return Response({
            'message': 'Payment Bill Already Analyzed',
            'data': 'This payment bill has already been processed and analyzed. Use the verification endpoint to modify the analyzed data.',
            'error_code': 'EXPENSE_BILL_ALREADY_PROCESSED'
        }, status=status.HTTP_200_OK)

    try:
        # Check if bill already has analyzed data
        if bill.analysed_data:
            logger.info(f"Using existing analyzed data for payment bill {bill_id}")
            analyzed_bill = process_existing_payment_analysis_data(bill, bill.analysed_data, organization)
        else:
            logger.info(f"Running new OpenAI analysis for payment bill {bill_id}")
            analysis_result = analyze_payment_bill_with_ai(bill, organization)
            if isinstance(analysis_result, dict) and not analysis_result.get('success'):
                return Response({
                    'error': 'Payment Bill Analysis Failed',
                    'message': 'The payment bill analysis could not be completed. This might be due to poor image quality, unsupported file format, or AI service issues.',
                    'details': analysis_result.get('error', 'Unknown error'),
                    'error_code': 'EXPENSE_ANALYSIS_FAILED'
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            analyzed_bill = analysis_result if not isinstance(analysis_result, dict) else analysis_result.get('analyzed_bill')

        # Check for duplicate bills after analysis
        is_duplicate, duplicate_bills, max_similarity = check_duplicate_tally_payment_bill(bill, organization)
        # Persist duplicate metadata so subsequent list/detail reads
        # surface the warning without re-running the fuzzy check.
        try:
            update_bill_duplicate_metadata(bill, duplicate_bills, max_similarity)
        except Exception as meta_err:
            logger.warning(f"Failed to persist payment duplicate metadata: {meta_err}")

        response_data = {
            "detail": "Tally payment bill analyzed successfully",
            "analyzed_bill": TallyPaymentAnalyzedBillSerializer(analyzed_bill).data
        }

        # Add duplicate warnings if found
        if is_duplicate:
            duplicate_warnings = []
            from apps.common.views import generate_signed_bill_file_url
            for dup in duplicate_bills:
                duplicate_bill = dup['bill']
                # Build bill URL — signed so the frontend viewer can load
                # it even though the media endpoint requires auth.
                bill_url = None
                if duplicate_bill.file:
                    try:
                        bill_url = generate_signed_bill_file_url(
                            duplicate_bill.file, request=request,
                        )
                    except Exception:
                        bill_url = None

                duplicate_warnings.append({
                    "duplicate_bill_id": str(duplicate_bill.id),
                    "duplicate_bill_name": duplicate_bill.bill_munshi_name,
                    "duplicate_bill_url": bill_url,
                    "similarity_score": round(dup['similarity_score'], 2),
                    "match_reasons": dup['match_reasons'],
                    "invoice_number": dup['invoice_number'],
                    "vendor_name": dup['vendor_name'],
                    "total": dup['total'],
                    "date": dup['date'],
                    "status": duplicate_bill.status
                })

            response_data.update({
                "duplicate_warning": True,
                "duplicate_count": len(duplicate_bills),
                "max_similarity": round(max_similarity, 2),
                "duplicate_bills": duplicate_warnings,
                "warning_message": f"⚠️ DUPLICATE DETECTED: Found {len(duplicate_bills)} similar Tally payment bill(s) in your organization. "
                                  f"This bill appears to be {round(max_similarity, 1)}% similar to existing bills. "
                                  "Please review carefully before proceeding to avoid duplicate entries."
            })

            logger.warning(f"Duplicate Tally payment bill detected for {bill.bill_munshi_name} - {len(duplicate_bills)} similar bills found")

        return Response(response_data, status=status.HTTP_200_OK)

    except Exception as e:
        logger.error(f"Payment bill analysis failed: {str(e)}")
        return Response({
            'error': 'Payment Bill Analysis Failed',
            'message': 'The payment bill analysis could not be completed. This might be due to poor image quality, unsupported file format, or AI service issues.',
            'details': str(e),
            'error_code': 'EXPENSE_ANALYSIS_FAILED'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


def process_existing_payment_analysis_data(bill, existing_data, organization):
    """Process existing analyzed data without calling OpenAI again"""
    try:
        logger.info(f"Processing existing analyzed data for payment bill {bill.id}")

        # Check if analyzed bill already exists
        try:
            analyzed_bill = TallyPaymentAnalyzedBill.objects.get(selected_bill=bill)
            logger.info(f"Found existing analyzed payment bill {analyzed_bill.id}")
            return analyzed_bill
        except TallyPaymentAnalyzedBill.DoesNotExist:
            pass

        # Extract required fields with safe access
        bill_number = extract_payment_bill_number(existing_data)
        date_issued = str(existing_data.get('dateIssued', ''))
        due_date_issued = str(existing_data.get('dueDate', ''))

        # Handle 'from' field safely
        from_data = existing_data.get('from', {})
        if isinstance(from_data, dict):
            company_name = str(from_data.get('name', '')).strip().lower()
        else:
            company_name = str(from_data).strip().lower()

        # Parse date with multiple format support
        bill_date = parse_payment_bill_date(date_issued)
        # Due date was never carried over on this path, so re-analysing an
        # existing payload silently dropped it. The fresh-analysis path has
        # always set it.
        due_date = parse_payment_bill_date(due_date_issued) if due_date_issued else None

        # Payment vouchers: Payable/Paid ledger stays blank so the user
        # picks a Bank/Cash ledger. See notes in ``process_payment_analysis_data``.
        vendor = None

        # Determine GST type with safe conversion and proper decimal rounding
        igst_val = _to_decimal(existing_data.get('igst', 0))
        cgst_val = _to_decimal(existing_data.get('cgst', 0))
        sgst_val = _to_decimal(existing_data.get('sgst', 0))
        tds_val = _to_decimal(existing_data.get('tds', 0))
        total_val = _to_decimal(existing_data.get('total', 0))

        if igst_val > 0:
            gst_type = TallyPaymentAnalyzedBill.GSTType.IGST
        elif cgst_val > 0 or sgst_val > 0:
            gst_type = TallyPaymentAnalyzedBill.GSTType.CGST_SGST
        else:
            gst_type = TallyPaymentAnalyzedBill.GSTType.UNKNOWN

        # 🏛️ FIND APPROPRIATE TAX LEDGERS FOR BILL-LEVEL GST VALUES
        logger.info(f"[EXISTING EXPENSE] Finding bill-level tax ledgers - IGST: {igst_val}, CGST: {cgst_val}, SGST: {sgst_val}")
        
        igst_tax_ledger = None
        cgst_tax_ledger = None
        sgst_tax_ledger = None
        
        if igst_val > 0:
            igst_tax_ledger = find_appropriate_payment_tax_ledger(organization, float(igst_val), 0, 0, ledger_type='bill')
        
        if cgst_val > 0:
            cgst_tax_ledger = find_appropriate_payment_tax_ledger(organization, 0, float(cgst_val), 0, ledger_type='bill')
        
        if sgst_val > 0:
            sgst_tax_ledger = find_appropriate_payment_tax_ledger(organization, 0, 0, float(sgst_val), ledger_type='bill')

        logger.info(f"[EXISTING EXPENSE] Tax ledgers found - IGST: {igst_tax_ledger}, CGST: {cgst_tax_ledger}, SGST: {sgst_tax_ledger}")

        # 🏛️ FIND EXPENSE COA LEDGER FOR LINE ITEMS
        payment_coa_ledger = find_appropriate_payment_tax_ledger(organization, 0, 0, 0, ledger_type='payment_coa')
        logger.info(f"[EXISTING EXPENSE] Payment COA ledger: {payment_coa_ledger}")

        # Create analyzed bill without Django validation
        with transaction.atomic():
            analyzed_bill = TallyPaymentAnalyzedBill(
                selected_bill=bill,
                vendor=vendor,
                bill_no=bill_number,
                bill_date=bill_date,
                due_date=due_date,
                igst=igst_val,
                cgst=cgst_val,
                sgst=sgst_val,
                tds=tds_val,
                total=total_val,
                igst_taxes=igst_tax_ledger,
                cgst_taxes=cgst_tax_ledger,
                sgst_taxes=sgst_tax_ledger,
                note="AI Analyzed Payment Bill (Existing Data)",
                organization=organization,
                gst_type=gst_type
            )

            # Save without calling clean() to skip validation
            analyzed_bill.save(skip_validation=True)

            # Create analyzed products (payment items) with auto-assigned COA ledger
            created_products = []
            payments = extract_payment_line_items(existing_data)
            if not payments:
                logger.warning(
                    "Payment bill %s: stored analysed_data carries no line items (keys: %s)",
                    bill.id, sorted(existing_data.keys()) if isinstance(existing_data, dict) else type(existing_data),
                )

            if isinstance(payments, list):
                for payment in payments:
                    if isinstance(payment, dict):
                        amount = _to_decimal(payment.get('amount', 0))

                        product = TallyPaymentAnalyzedProduct(
                            payment_bill=analyzed_bill,
                            item_details=str(payment.get('description', '')),
                            chart_of_accounts=payment_coa_ledger,
                            amount=amount,
                            debit_or_credit=TallyPaymentAnalyzedProduct.DebitCredit.DEBIT,
                            organization=organization
                        )
                        created_products.append(product)

            # Bulk create products
            if created_products:
                TallyPaymentAnalyzedProduct.objects.bulk_create(created_products)
                logger.info(f"Successfully created {len(created_products)} payment products for bill {analyzed_bill.id}")

                # ✅ CREATE CONSOLIDATED PRODUCTS FOR LAYOUT SWITCHING SUPPORT
                if len(created_products) > 1:
                    try:
                        total_amount = sum(p.amount for p in created_products)
                        items_count = len(created_products)

                        item_details = []
                        for product in created_products:
                            item_details.append(f'• {product.item_details} (Amount: ₹{product.amount})')

                        consolidated_details = f'Consolidated {items_count} payment entries:\n' + '\n'.join(item_details)

                        consolidated_product = TallyPaymentConsolidatedProduct.objects.create(
                            payment_bill=analyzed_bill,
                            organization=organization,
                            item_details=consolidated_details,
                            chart_of_accounts=payment_coa_ledger,
                            amount=total_amount,
                            debit_or_credit=TallyPaymentConsolidatedProduct.DebitCredit.DEBIT,
                            original_entries_count=items_count,
                            consolidation_notes=f'Auto-created during existing data processing for {items_count} payment entries'
                        )

                        logger.info(f"✅ Created consolidated payment product for bill {analyzed_bill.id} with {items_count} entries (₹{total_amount})")

                        if not analyzed_bill.consolidate:
                            analyzed_bill.consolidate = True
                            analyzed_bill.save(update_fields=['consolidate'])

                    except Exception as e:
                        logger.error(f"❌ Error creating consolidated payment product for bill {analyzed_bill.id}: {str(e)}")
                else:
                    logger.info(f"ℹ️ Bill has only {len(created_products)} item - no consolidation needed")

            # Update bill status
            bill.status = TallyPaymentBill.BillStatus.ANALYSED
            bill.process = True
            bill.save(update_fields=['status', 'process'])

            logger.info(f"Successfully processed existing payment analysis data for bill {bill.id}")
            return analyzed_bill

    except Exception as e:
        logger.error(f"Error processing existing payment analysis data: {str(e)}")
        raise Exception(f"Error processing existing payment analysis data: {str(e)}")


# ============================================================================
# Get Payment Bill Detail
# ✅
@extend_schema(
    summary="Get Payment Bill Detail",
    description="Get detailed information about a specific payment bill including analysis data",
    responses={200: "TallyPaymentBillDetailSerializer"},
    tags=['Tally Payment Bills']
)
@api_view(['GET'])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def payment_bill_detail(request, org_id, bill_id):
    """Get payment bill detail including analysis data using proper serializer"""
    from ..serializers import TallyPaymentBillDetailSerializer
    from .bill_helpers import bill_detail_base
    return bill_detail_base(request, org_id, bill_id, TallyPaymentBill, TallyPaymentBillDetailSerializer)


# ===========================================================================
# Payment Bill Verify View
# ✅
@extend_schema(
    summary="Verify Payment Bill",
    description="Verify analyzed payment bill data and mark as verified",
    request=PaymentBillVerificationSerializer,
    responses={200: TallyPaymentAnalyzedBillSerializer},
    tags=['Tally Payment Bills']
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def payment_bill_verify(request, org_id):
    """Verify analyzed payment bill with user modifications"""
    bill_id = request.data.get('bill_id')
    analyzed_bill_id = request.data.get('analyzed_bill')
    analyzed_data = request.data.get('analyzed_data')

    organization = get_organization_from_request(request, org_id)
    if not organization:
        return Response(
            {
                'error': 'Organization Access Denied',
                'message': f'Organization with ID {org_id} not found or you do not have access to it. Please check the organization ID and your permissions.',
                'error_code': 'ORG_NOT_FOUND'
            },
            status=status.HTTP_404_NOT_FOUND
        )

    if not bill_id or not analyzed_bill_id:
        return Response(
            {
                'error': 'Missing Required Parameters',
                'message': 'Both bill_id and analyzed_bill parameters are required for payment bill verification. Please provide both values.',
                'error_code': 'REQUIRED_PARAMS_MISSING'
            },
            status=status.HTTP_422_UNPROCESSABLE_ENTITY
        )

    try:
        bill = TallyPaymentBill.objects.alive().get(id=bill_id, organization=organization)
        analyzed_bill = TallyPaymentAnalyzedBill.objects.get(id=analyzed_bill_id, organization=organization)
    except (TallyPaymentBill.DoesNotExist, TallyPaymentAnalyzedBill.DoesNotExist):
        return Response(
            {
                'error': 'Payment Bill or Analysis Data Not Found',
                'message': f'Payment bill with ID {bill_id} or its analyzed data not found. Please ensure the payment bill exists and has been analyzed.',
                'error_code': 'EXPENSE_BILL_OR_ANALYSIS_NOT_FOUND'
            },
            status=status.HTTP_404_NOT_FOUND
        )

    # Allow re-verification of Synced bills only if tally_synced is still False
    allowed_statuses = [TallyPaymentBill.BillStatus.ANALYSED, TallyPaymentBill.BillStatus.VERIFIED]
    if bill.status == TallyPaymentBill.BillStatus.SYNCED and not bill.tally_synced:
        allowed_statuses.append(TallyPaymentBill.BillStatus.SYNCED)

    if bill.status not in allowed_statuses:
        return Response(
            {
                'error': 'Invalid Payment Bill Status',
                'message': f'Payment bill must be in "Analysed", "Verified", or "Synced" (not yet posted to Tally) status to perform verification. Current status: {bill.status}',
                'current_status': bill.status,
                'required_status': ['Analysed', 'Verified', 'Synced (not posted)'],
                'error_code': 'INVALID_EXPENSE_BILL_STATUS'
            },
            status=status.HTTP_422_UNPROCESSABLE_ENTITY
        )

    try:
        # Update the analyzed bill with user modifications
        verified_bill = update_analyzed_payment_bill_data(analyzed_bill, analyzed_data, organization)

        # Recompute round-off after products & tax fields are persisted so the
        # journal entry can balance in Tally.
        try:
            verified_bill.compute_round_off()
        except Exception as round_off_err:
            logger.warning(
                f"Round-off computation failed for payment bill {verified_bill.id}: {round_off_err}"
            )

        # Update bill status to verified
        bill.status = TallyPaymentBill.BillStatus.VERIFIED
        bill.save(update_fields=['status'])

        # Return the updated data in the same structured format
        response_data = get_structured_payment_bill_data(verified_bill, organization)

        return Response({
            "message": "Payment bill verified successfully",
            "analyzed_data": response_data
        }, status=status.HTTP_200_OK)

    except Exception as e:
        logger.error(f"Payment bill verification failed: {str(e)}")
        return Response({
            'error': 'Payment Bill Verification Failed',
            'message': 'The payment bill verification process encountered an error. This could be due to invalid data or debit/credit imbalance. Please check your entries and try again.',
            'details': str(e),
            'error_code': 'EXPENSE_VERIFICATION_FAILED'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


def update_analyzed_payment_bill_data(analyzed_bill, analyzed_data, organization):
    """Update analyzed payment bill with user modifications"""

    if not analyzed_data:
        return analyzed_bill
    
    # Add defensive check to ensure analyzed_bill is the correct type
    from ..models import TallyPaymentAnalyzedBill
    if not isinstance(analyzed_bill, TallyPaymentAnalyzedBill):
        logger.error(f"Expected TallyPaymentAnalyzedBill, got {type(analyzed_bill)}")
        raise ValueError(f"Invalid analyzed_bill type: {type(analyzed_bill)}")

    with transaction.atomic():
        # Payment voucher: user explicitly picks the Bank/Cash ledger
        # from a dropdown, so ``vendor_id`` is authoritative. Fall back
        # to name-based lookup only if the frontend sends a name
        # instead (legacy shape).
        vendor_id = analyzed_data.get('vendor_id') or analyzed_data.get('vendor')
        if vendor_id:
            try:
                analyzed_bill.vendor = Ledger.objects.get(
                    id=vendor_id, organization=organization,
                )
            except (Ledger.DoesNotExist, ValueError, DjangoValidationError):
                logger.warning(
                    "Payment verify: submitted vendor_id %s not found in org %s",
                    vendor_id, organization.id,
                )
        elif analyzed_data.get('name'):
            vendor_name = analyzed_data.get('name')
            if vendor_name and vendor_name != "No Ledger":
                vendor = find_or_create_payment_vendor_ledger(vendor_name, {}, organization)
                if vendor:
                    analyzed_bill.vendor = vendor

        # Payment Mode — the Bank/Cash ledger the payment is actually made
        # through (Correction 26). FE sends the UUID explicitly; it is
        # scoped to TallyConfig.payment_parents on the frontend dropdown,
        # but any org ledger is accepted here (mirrors the vendor_id
        # handling above — trust the FE-picked UUID).
        if 'payment_mode_id' in analyzed_data:
            payment_mode_id = analyzed_data.get('payment_mode_id')
            if payment_mode_id:
                try:
                    analyzed_bill.payment_mode = Ledger.objects.get(
                        id=payment_mode_id, organization=organization,
                    )
                except (Ledger.DoesNotExist, ValueError, DjangoValidationError):
                    logger.warning(
                        "Payment verify: submitted payment_mode_id %s not found in org %s",
                        payment_mode_id, organization.id,
                    )
            else:
                analyzed_bill.payment_mode = None

        # Update vendor debit_or_credit if provided
        if 'vendor_debit_or_credit' in analyzed_data:
            analyzed_bill.vendor_debit_or_credit = analyzed_data['vendor_debit_or_credit']

        # Update vendor_amount if provided
        if 'vendor_amount' in analyzed_data:
            analyzed_bill.vendor_amount = _to_decimal(analyzed_data['vendor_amount'])

        # Update bill details - handle flattened structure
        if 'voucher' in analyzed_data:
            analyzed_bill.voucher = analyzed_data['voucher']
        if 'bill_no' in analyzed_data:
            analyzed_bill.bill_no = analyzed_data['bill_no']
        if 'bill_date' in analyzed_data:
            # Parse date string (format: "31-12-2023")
            bill_date = parse_payment_bill_date(analyzed_data['bill_date'])
            if bill_date:
                analyzed_bill.bill_date = bill_date
        if 'due_date' in analyzed_data:
            # Empty string / null = explicit clear from the FE.
            raw_due = analyzed_data['due_date']
            if raw_due in (None, ''):
                analyzed_bill.due_date = None
            else:
                due_date = parse_payment_bill_date(raw_due)
                if due_date:
                    analyzed_bill.due_date = due_date
        if 'total' in analyzed_data:
            analyzed_bill.total = _to_decimal(analyzed_data['total'])

        # Update tax information — unified 6-block loop.
        # Each block: (payload key, amount field, FK field, tax_type,
        #              debit/credit field). Zero amount clears the FK so a
        #              re-verify with a cleared row does not keep the stale
        #              ledger + trip the master-sync guard.
        taxes_data = analyzed_data.get('taxes', {})
        if taxes_data:
            _TAX_BLOCKS = (
                ('igst', 'igst', 'igst_taxes', 'IGST', 'igst_debit_or_credit'),
                ('cgst', 'cgst', 'cgst_taxes', 'CGST', 'cgst_debit_or_credit'),
                ('sgst', 'sgst', 'sgst_taxes', 'SGST', 'sgst_debit_or_credit'),
                ('tds', 'tds', 'tds_taxes', 'TDS', 'tds_debit_or_credit'),
                ('other_adjustment', 'other_adjustment', 'other_adjustment_taxes',
                 'OTHER', 'other_adjustment_debit_or_credit'),
                ('round_off', 'round_off', 'round_off_taxes',
                 'ROUND_OFF', 'round_off_debit_or_credit'),
            )
            for key, amount_field, fk_field, tax_type, dc_field in _TAX_BLOCKS:
                block = taxes_data.get(key) or {}
                if not isinstance(block, dict):
                    continue
                if 'amount' in block:
                    setattr(analyzed_bill, amount_field, _to_decimal(block['amount']))
                # Always resolve the ledger — user may pick a new ledger
                # while amount is temporarily 0 during edit.
                if 'ledger' in block or 'ledger_id' in block:
                    ledger = _resolve_payment_tax_ledger(block, tax_type, organization)
                    if ledger is not None:
                        setattr(analyzed_bill, fk_field, ledger)
                    elif _is_blank_payment_ledger(block.get('ledger')) and not block.get('ledger_id'):
                        setattr(analyzed_bill, fk_field, None)
                if dc_field in ('igst_debit_or_credit', 'cgst_debit_or_credit',
                                'sgst_debit_or_credit', 'tds_debit_or_credit',
                                'other_adjustment_debit_or_credit',
                                'round_off_debit_or_credit') and 'debit_or_credit' in block:
                    setattr(analyzed_bill, dc_field, block['debit_or_credit'])
                # Final-state rule: null the FK when amount is 0/empty so
                # the sync payload doesn't emit a ghost ledger row.
                final_amt = getattr(analyzed_bill, amount_field, None)
                if not final_amt or final_amt == 0:
                    setattr(analyzed_bill, fk_field, None)

        # ------------------------------------------------------------------
        # Multi-rate GST lines — replaces the single bill-level CGST/SGST/IGST
        # values. The frontend now sends ``analyzed_data.gst_lines`` as an
        # array; we wipe + recreate so the UI is authoritative. Each row
        # becomes a ``<ledger>`` entry in the sync XML.
        #
        # The legacy bill-level cgst/sgst/igst fields ARE still updated
        # below as a rollup sum so the deprecated readers keep working for
        # one release. They'll be dropped in a follow-up migration.
        # ------------------------------------------------------------------
        gst_lines_payload = analyzed_data.get('gst_lines')
        if isinstance(gst_lines_payload, list):
            # Wipe existing rows (the array is the source of truth).
            TallyPaymentGstLine.objects.filter(payment_bill=analyzed_bill).delete()

            for entry in gst_lines_payload:
                if not isinstance(entry, dict):
                    continue
                amount = _to_decimal(entry.get('amount'))
                tax_type = (entry.get('tax_type') or '').upper().strip()
                if amount <= 0 or tax_type not in ('CGST', 'SGST', 'IGST'):
                    continue
                # Resolve the ledger FK from either an explicit ID or
                # a name (the name path mirrors the existing tax-ledger
                # helpers used above for CGST/SGST/IGST).
                ledger = None
                ledger_id = entry.get('ledger')
                if ledger_id and not isinstance(ledger_id, (dict, list)):
                    # ID-shaped — try a direct lookup first.
                    try:
                        ledger = Ledger.objects.get(
                            id=ledger_id, organization=organization,
                        )
                    except (Ledger.DoesNotExist, ValueError, DjangoValidationError):
                        # Fallback: treat the value as a name.
                        ledger = find_or_create_payment_tax_ledger(
                            str(ledger_id), tax_type, organization,
                        )
                dc = (entry.get('debit_or_credit') or 'debit').lower().strip()
                if dc not in ('debit', 'credit'):
                    dc = 'debit'

                TallyPaymentGstLine.objects.create(
                    organization=organization,
                    payment_bill=analyzed_bill,
                    rate=(entry.get('rate') or '').strip(),
                    tax_type=tax_type,
                    amount=amount,
                    ledger=ledger,
                    debit_or_credit=dc,
                )

            # Roll up the gst_lines into the deprecated bill-level
            # cgst/sgst/igst fields so legacy code paths still see a
            # consistent value during the transition window.
            gst_lines_qs = TallyPaymentGstLine.objects.filter(payment_bill=analyzed_bill)
            sums = {'CGST': Decimal('0'), 'SGST': Decimal('0'), 'IGST': Decimal('0')}
            ledgers_by_type = {'CGST': None, 'SGST': None, 'IGST': None}
            for line in gst_lines_qs:
                sums[line.tax_type] += line.amount or Decimal('0')
                # Stash the *first* ledger per tax_type so legacy single-FK
                # readers get a sensible value (the new XML emitter doesn't
                # use this — it iterates gst_lines directly).
                if not ledgers_by_type[line.tax_type] and line.ledger_id:
                    ledgers_by_type[line.tax_type] = line.ledger
            analyzed_bill.cgst = sums['CGST']
            analyzed_bill.sgst = sums['SGST']
            analyzed_bill.igst = sums['IGST']
            if ledgers_by_type['CGST']:
                analyzed_bill.cgst_taxes = ledgers_by_type['CGST']
            if ledgers_by_type['SGST']:
                analyzed_bill.sgst_taxes = ledgers_by_type['SGST']
            if ledgers_by_type['IGST']:
                analyzed_bill.igst_taxes = ledgers_by_type['IGST']

        # Determine GST type — bill-level first, gst_lines fallback.
        _bill_igst = analyzed_bill.igst or 0
        _bill_cgst = analyzed_bill.cgst or 0
        _bill_sgst = analyzed_bill.sgst or 0
        if _bill_igst > 0:
            analyzed_bill.gst_type = TallyPaymentAnalyzedBill.GSTType.IGST
        elif _bill_cgst > 0 or _bill_sgst > 0:
            analyzed_bill.gst_type = TallyPaymentAnalyzedBill.GSTType.CGST_SGST
        else:
            try:
                _lines = TallyPaymentGstLine.objects.filter(payment_bill=analyzed_bill)
                _line_igst = sum((ln.amount or Decimal('0')) for ln in _lines if ln.tax_type == 'IGST')
                _line_cgst = sum((ln.amount or Decimal('0')) for ln in _lines if ln.tax_type == 'CGST')
                _line_sgst = sum((ln.amount or Decimal('0')) for ln in _lines if ln.tax_type == 'SGST')
            except Exception:
                _line_igst = _line_cgst = _line_sgst = Decimal('0')
            if _line_igst > 0:
                analyzed_bill.gst_type = TallyPaymentAnalyzedBill.GSTType.IGST
            elif _line_cgst > 0 or _line_sgst > 0:
                analyzed_bill.gst_type = TallyPaymentAnalyzedBill.GSTType.CGST_SGST
            else:
                analyzed_bill.gst_type = TallyPaymentAnalyzedBill.GSTType.UNKNOWN

        # Save the analyzed bill
        analyzed_bill.save(skip_validation=True)

        # Handle consolidation flag first to determine which data to process
        consolidate_flag = analyzed_data.get('consolidate', False)
        if consolidate_flag != getattr(analyzed_bill, 'consolidate', False):
            analyzed_bill.consolidate = consolidate_flag
            analyzed_bill.save()

        # 🔄 CONSOLIDATION LOGIC - Match vendor pattern exactly
        if consolidate_flag:
            # CONSOLIDATED MODE: Handle consolidate_prod array
            logger.info("Processing in consolidated products mode")
            consolidate_prod_array = analyzed_data.get('consolidate_prod', [])
            
            if consolidate_prod_array:
                try:
                    # Clear existing consolidated products to prevent duplicates
                    existing_consolidated = TallyPaymentConsolidatedProduct.objects.filter(payment_bill=analyzed_bill)
                    if existing_consolidated.exists():
                        existing_count = existing_consolidated.count()
                        existing_consolidated.delete()
                        logger.info(f"Deleted {existing_count} existing consolidated products before creating new ones")

                    # Create new consolidated products from payload
                    for idx, consolidated_data in enumerate(consolidate_prod_array):
                        logger.info(f"Creating consolidated payment product {idx + 1}: {consolidated_data.get('item_details', 'Unnamed')}")
                        
                        # Find chart of accounts ledger — UUID first, name fallback.
                        chart_ledger = None
                        chart_id = consolidated_data.get('chart_of_accounts_id')
                        chart_raw = consolidated_data.get('chart_of_accounts')
                        if chart_id:
                            try:
                                chart_ledger = Ledger.objects.filter(
                                    id=chart_id, organization=organization,
                                ).first()
                            except (ValueError, DjangoValidationError):
                                chart_ledger = None
                        if chart_ledger is None and chart_raw and not _is_blank_payment_ledger(chart_raw):
                            try:
                                chart_ledger = Ledger.objects.filter(
                                    id=str(chart_raw).strip(), organization=organization,
                                ).first()
                            except (ValueError, DjangoValidationError):
                                chart_ledger = None
                            if chart_ledger is None:
                                chart_ledger = Ledger.objects.filter(
                                    name=str(chart_raw).strip(),
                                    organization=organization,
                                ).first()

                        # Create new consolidated product
                        consolidated_product = TallyPaymentConsolidatedProduct.objects.create(
                            payment_bill=analyzed_bill,
                            organization=organization,
                            item_details=consolidated_data.get('item_details', 'Consolidated payment from verification'),
                            amount=_to_decimal(consolidated_data.get('amount', 0)),
                            debit_or_credit=consolidated_data.get('debit_or_credit', 'debit'),
                            chart_of_accounts=chart_ledger,
                            original_entries_count=consolidated_data.get('original_entries_count', 1),
                            consolidation_notes='Created from frontend verification'
                        )
                        logger.info(f"Created consolidated payment product {consolidated_product.id} with chart of accounts: {chart_ledger.name if chart_ledger else 'None'}")
                        
                    # ✅ CRITICAL: Keep individual products intact for layout switching support
                    # Individual products are NEVER deleted in consolidation mode
                    existing_individual_products = analyzed_bill.products.all()
                    if existing_individual_products.exists():
                        logger.info(f"✅ PRESERVED {existing_individual_products.count()} individual products (layout switching support)")
                        logger.info("✅ Users can switch between individual and consolidated view")
                    else:
                        logger.warning("No individual products found to preserve")
                        
                except Exception as consolidate_error:
                    logger.error(f"Error processing consolidate_prod array: {consolidate_error}")
                    
            else:
                logger.warning("Consolidate=true but no consolidate_prod array found in payload")
                # Keep individual products intact even if consolidate_prod is missing
                existing_individual_products = analyzed_bill.products.all()
                if existing_individual_products.exists():
                    logger.info(f"✅ PRESERVED {existing_individual_products.count()} individual products (no consolidate_prod in payload)")
                
        else:
            # INDIVIDUAL PRODUCTS MODE: Handle payment_items array
            logger.info("Processing in individual products mode")
            payment_items = analyzed_data.get('payment_items', [])
            
            if payment_items:
                logger.info(f"Processing {len(payment_items)} individual payment items")
                update_analyzed_payment_products(analyzed_bill, payment_items, organization)
                
                # Keep any existing consolidated products for layout switching support
                existing_consolidated_products = analyzed_bill.consolidated_products.all()
                if existing_consolidated_products.exists():
                    logger.info(f"✅ PRESERVED {existing_consolidated_products.count()} consolidated products (layout switching support)")
                    # Consolidated products are preserved for layout flexibility

        return analyzed_bill


def find_or_create_payment_chart_of_accounts_ledger(coa_name, organization):
    """Delegates to shared find_coa_ledger in helpers."""
    from .helpers import find_coa_ledger
    return find_coa_ledger(coa_name, organization)


def find_or_create_payment_vendor_ledger(vendor_name, vendor_data, organization):
    """Delegates to shared find_or_create_tally_vendor_ledger in helpers."""
    from .helpers import find_or_create_tally_vendor_ledger
    return find_or_create_tally_vendor_ledger(
        vendor_name, vendor_data, organization,
        default_parent_name=None, create_default_parent=False
    )


def find_or_create_payment_tax_ledger(ledger_name, tax_type, organization):
    """Delegates to shared find_or_create_tally_tax_ledger in helpers."""
    from .helpers import find_or_create_tally_tax_ledger
    return find_or_create_tally_tax_ledger(ledger_name, tax_type, organization)


def update_analyzed_payment_products(analyzed_bill, payment_items, organization):
    """Update existing payment products and create new ones based on item_id"""

    # Validate debit/credit balance before processing - including all components
    total_debit = 0
    total_credit = 0

    # Calculate debit/credit from payment items
    for item_data in payment_items:
        amount = float(_to_decimal(item_data.get('amount', 0)))
        debit_or_credit = item_data.get('debit_or_credit', '').lower()

        if debit_or_credit == 'debit':
            total_debit += amount
        elif debit_or_credit == 'credit':
            total_credit += amount

    # Add tax amounts to debit/credit totals
    if analyzed_bill.igst and analyzed_bill.igst > 0:
        if analyzed_bill.igst_debit_or_credit == 'debit':
            total_debit += float(analyzed_bill.igst)
        elif analyzed_bill.igst_debit_or_credit == 'credit':
            total_credit += float(analyzed_bill.igst)

    if analyzed_bill.cgst and analyzed_bill.cgst > 0:
        if analyzed_bill.cgst_debit_or_credit == 'debit':
            total_debit += float(analyzed_bill.cgst)
        elif analyzed_bill.cgst_debit_or_credit == 'credit':
            total_credit += float(analyzed_bill.cgst)

    if analyzed_bill.sgst and analyzed_bill.sgst > 0:
        if analyzed_bill.sgst_debit_or_credit == 'debit':
            total_debit += float(analyzed_bill.sgst)
        elif analyzed_bill.sgst_debit_or_credit == 'credit':
            total_credit += float(analyzed_bill.sgst)

    # Add TDS amounts to debit/credit totals
    if analyzed_bill.tds and analyzed_bill.tds > 0:
        if analyzed_bill.tds_debit_or_credit == 'debit':
            total_debit += float(analyzed_bill.tds)
        elif analyzed_bill.tds_debit_or_credit == 'credit':
            total_credit += float(analyzed_bill.tds)

    # Add vendor amount to debit/credit totals
    if analyzed_bill.vendor_amount and analyzed_bill.vendor_amount > 0:
        if analyzed_bill.vendor_debit_or_credit == 'debit':
            total_debit += float(analyzed_bill.vendor_amount)
        elif analyzed_bill.vendor_debit_or_credit == 'credit':
            total_credit += float(analyzed_bill.vendor_amount)

    # Include other_adjustment + round_off (mirror expense fix).
    if analyzed_bill.other_adjustment and analyzed_bill.other_adjustment != 0:
        amt = abs(float(analyzed_bill.other_adjustment))
        if analyzed_bill.other_adjustment_debit_or_credit == 'debit':
            total_debit += amt
        elif analyzed_bill.other_adjustment_debit_or_credit == 'credit':
            total_credit += amt

    if analyzed_bill.round_off and analyzed_bill.round_off != 0:
        amt = abs(float(analyzed_bill.round_off))
        if analyzed_bill.round_off_debit_or_credit == 'debit':
            total_debit += amt
        elif analyzed_bill.round_off_debit_or_credit == 'credit':
            total_credit += amt

    # Check if debit and credit amounts are equal (including all components)
    # (allowing for small rounding differences)
    if abs(total_debit - total_credit) > 0.01:
        raise Exception(
            f"Total Debit and Credit amounts must be equal across all components. "
            f"Total Debit: {total_debit}, Total Credit: {total_credit}, "
            f"Difference: {abs(total_debit - total_credit)}. "
            f"This includes payment items, taxes (IGST/CGST/SGST/TDS), "
            f"vendor amount, other adjustment and round off."
        )

    # Get existing products mapped by their ID
    existing_products = {str(p.id): p for p in analyzed_bill.products.all()}
    updated_product_ids = set()

    for item_data in payment_items:
        item_id = item_data.get('item_id')  # Check for item_id in payload

        if item_id and str(item_id) in existing_products:
            # Update existing product
            product = existing_products[str(item_id)]
            updated_product_ids.add(str(item_id))
        else:
            # Create new product if item_id is missing or doesn't match existing
            product = TallyPaymentAnalyzedProduct(
                payment_bill=analyzed_bill,
                organization=organization
            )

        # Update product fields
        if 'item_details' in item_data:
            product.item_details = item_data['item_details']
        if 'amount' in item_data:
            product.amount = _to_decimal(item_data['amount'])
        if 'debit_or_credit' in item_data:
            product.debit_or_credit = item_data['debit_or_credit']

        # Handle chart of accounts ledger — UUID first, name fallback.
        # BE guard is now case-insensitive; a "no coa ledger" round-trip
        # no longer spawns a bogus ledger.
        coa_id = item_data.get('chart_of_accounts_id')
        coa_raw = item_data.get('chart_of_accounts')
        coa_ledger = None
        if coa_id:
            try:
                coa_ledger = Ledger.objects.get(id=coa_id, organization=organization)
            except (Ledger.DoesNotExist, ValueError, DjangoValidationError):
                coa_ledger = None
        if coa_ledger is None and coa_raw and not _is_blank_payment_ledger(coa_raw):
            try:
                coa_ledger = Ledger.objects.get(id=str(coa_raw).strip(), organization=organization)
            except (Ledger.DoesNotExist, ValueError, DjangoValidationError):
                coa_ledger = find_or_create_payment_tax_ledger(
                    str(coa_raw).strip(), 'COA', organization,
                )
        if coa_ledger is not None:
            product.chart_of_accounts = coa_ledger
        elif coa_id is None and _is_blank_payment_ledger(coa_raw):
            product.chart_of_accounts = None

        product.save()

    # Delete removed products in BOTH modes. Previously gated on
    # `consolidate=False` so a user in consolidate mode could remove rows
    # from the individual list but the DB kept them — reload showed stale
    # rows. Consolidate view now derives from `consolidated_products`, not
    # the individual list, so pruning here is safe.
    # Safety: in consolidate mode the FE may hold aggregated rows in
    # `payment_items` state; saving in that state used to wipe every
    # real individual product because none of the fake-ids matched.
    is_consolidate_view_only = (
        getattr(analyzed_bill, 'consolidate', False)
        and existing_products
        and not (updated_product_ids & set(existing_products.keys()))
    )
    if is_consolidate_view_only:
        logger.info(
            "Skipping individual-product delete on payment bill %s — payload "
            "appears to be from the consolidated view (no incoming id matches).",
            analyzed_bill.id,
        )
        products_to_delete = []
    else:
        products_to_delete = [
            product for existing_id, product in existing_products.items()
            if existing_id not in updated_product_ids
        ]
    deletion_count = len(products_to_delete)
    if products_to_delete:
        for product in products_to_delete:
            logger.info(f"Deleting payment product {product.id}: {product.item_details or 'Unknown'}")
            product.delete()
        # A line change invalidates the consolidated aggregation — wipe it
        # so the next reload does not surface stale consolidated rows.
        stale_consolidated = TallyPaymentConsolidatedProduct.objects.filter(
            payment_bill=analyzed_bill,
        )
        if stale_consolidated.exists():
            stale_count = stale_consolidated.count()
            stale_consolidated.delete()
            logger.info(
                f"Wiped {stale_count} stale consolidated rows after line change"
            )
    
    logger.info(
        f"Payment product update summary: {len(updated_product_ids)} updated, "
        f"{len(payment_items or []) - len(updated_product_ids)} created, "
        f"{deletion_count} deleted (consolidate mode: {getattr(analyzed_bill, 'consolidate', False)})"
    )


def get_structured_payment_bill_data(analyzed_bill, organization):
    """Get structured payment bill data in the same format as detail view.

    The FE detail page reads *flat* keys (``bill_no``, ``bill_date``,
    ``total_amount``, ...) so the same fields are mirrored at the top
    level in addition to the legacy nested ``bill_details`` block. A
    reload that dropped these keys would leave the UI blank.
    """
    vendor_ledger = analyzed_bill.vendor
    payment_mode_ledger = analyzed_bill.payment_mode
    analyzed_bill_products = analyzed_bill.products.all()
    bill_date_str = analyzed_bill.bill_date.strftime('%d-%m-%Y') if analyzed_bill.bill_date else None
    due_date_str = analyzed_bill.due_date.strftime('%d-%m-%Y') if analyzed_bill.due_date else None
    team_slug = organization.name if hasattr(organization, 'name') else str(organization.id)

    def _tax_block(amount, ledger_fk, dc):
        return {
            "amount": float(amount or 0),
            "ledger": str(ledger_fk) if ledger_fk else "",
            "ledger_id": str(ledger_fk.id) if ledger_fk else None,
            "debit_or_credit": dc or "debit",
        }

    consolidated_prod = [
        {
            "id": str(cp.id),
            "item_details": cp.item_details,
            "chart_of_accounts": str(cp.chart_of_accounts.name) if cp.chart_of_accounts else "",
            "chart_of_accounts_id": str(cp.chart_of_accounts_id) if cp.chart_of_accounts_id else None,
            "amount": float(cp.amount or 0),
            "debit_or_credit": cp.debit_or_credit or "debit",
        }
        for cp in analyzed_bill.consolidated_products.all()
    ]

    return {
        # Flat mirror keys the FE detail hydration reads.
        "bill_no": analyzed_bill.bill_no or "",
        "bill_date": bill_date_str,
        "due_date": due_date_str,
        "total_amount": float(analyzed_bill.total or 0),
        "note": analyzed_bill.note or "",
        "consolidate": bool(getattr(analyzed_bill, "consolidate", False)),

        "vendor": {
            "id": str(vendor_ledger.id) if vendor_ledger else None,
            "master_id": vendor_ledger.master_id if vendor_ledger and vendor_ledger.master_id else "",
            "name": vendor_ledger.name if vendor_ledger and vendor_ledger.name else "",
            "vendor_name": vendor_ledger.name if vendor_ledger and vendor_ledger.name else "",
            "gst_in": vendor_ledger.gst_in if vendor_ledger and vendor_ledger.gst_in else "",
            "company": vendor_ledger.company if vendor_ledger and vendor_ledger.company else "",
        },
        # Bank/Cash ledger the payment is made through (Correction 26).
        # Emitted as the DEBIT entry in the sync XML.
        "payment_mode": {
            "id": str(payment_mode_ledger.id) if payment_mode_ledger else None,
            "name": payment_mode_ledger.name if payment_mode_ledger and payment_mode_ledger.name else "",
        },
        "bill_details": {
            "voucher": analyzed_bill.voucher or "",
            "bill_number": analyzed_bill.bill_no,
            "date": bill_date_str,
            "due_date": due_date_str,
            "total_amount": float(analyzed_bill.total or 0),
            "company_id": team_slug,
        },
        "taxes": {
            "igst": _tax_block(analyzed_bill.igst, analyzed_bill.igst_taxes, analyzed_bill.igst_debit_or_credit),
            "cgst": _tax_block(analyzed_bill.cgst, analyzed_bill.cgst_taxes, analyzed_bill.cgst_debit_or_credit),
            "sgst": _tax_block(analyzed_bill.sgst, analyzed_bill.sgst_taxes, analyzed_bill.sgst_debit_or_credit),
            "tds": _tax_block(analyzed_bill.tds, analyzed_bill.tds_taxes, analyzed_bill.tds_debit_or_credit),
            "other_adjustment": _tax_block(
                analyzed_bill.other_adjustment,
                analyzed_bill.other_adjustment_taxes,
                analyzed_bill.other_adjustment_debit_or_credit,
            ),
            "round_off": _tax_block(
                analyzed_bill.round_off,
                analyzed_bill.round_off_taxes,
                analyzed_bill.round_off_debit_or_credit,
            ),
        },
        "payment_items": [
            {
                "id": str(item.id),
                "item_details": item.item_details,
                "chart_of_accounts": str(item.chart_of_accounts.name) if item.chart_of_accounts else "",
                "chart_of_accounts_id": str(item.chart_of_accounts_id) if item.chart_of_accounts_id else None,
                "amount": float(item.amount or 0),
                "debit_or_credit": item.debit_or_credit,
            }
            for item in analyzed_bill_products
        ],
        "consolidate_prod": consolidated_prod,
        # Multi-rate GST lines — new source of truth. The `taxes` block
        # above keeps the legacy single-value readers working.
        "gst_lines": [
            {
                "id": str(line.id),
                "rate": line.rate or "",
                "tax_type": line.tax_type,
                "amount": float(line.amount or 0),
                "ledger_id": str(line.ledger_id) if line.ledger_id else None,
                "ledger": str(line.ledger_id) if line.ledger_id else None,
                "ledger_name": line.ledger.name if line.ledger else "",
                "debit_or_credit": line.debit_or_credit or "debit",
            }
            for line in analyzed_bill.gst_lines.all()
        ],
    }


# ============================================================================
# Payment Bill Sync View
# ✅
@extend_schema(
    summary="Sync Payment Bill",
    description="Sync verified payment bill with Tally system",
    request=PaymentBillSyncRequestSerializer,
    responses={200: PaymentBillSyncResponseSerializer},
    tags=['Tally Payment Bills']
)
@api_view(['POST'])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def payment_bill_sync(request, org_id):
    """Sync verified payment bill with Tally"""
    serializer = PaymentBillSyncRequestSerializer(data=request.data)
    if not serializer.is_valid():
        return Response({
            'error': 'Invalid Sync Request',
            'message': 'The payment bill sync request data is invalid. Please ensure the bill_id is provided and valid.',
            'details': serializer.errors,
            'error_code': 'INVALID_EXPENSE_SYNC_REQUEST'
        }, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

    bill_id = serializer.validated_data['bill_id']
    organization = get_organization_from_request(request, org_id)

    try:
        bill = TallyPaymentBill.objects.alive().get(id=bill_id, organization=organization)
        analyzed_bill = TallyPaymentAnalyzedBill.objects.get(selected_bill=bill)
    except (TallyPaymentBill.DoesNotExist, TallyPaymentAnalyzedBill.DoesNotExist):
        return Response({
            'error': 'Payment Bill or Analysis Data Not Found',
            'message': f'Payment bill with ID {bill_id} or its analyzed data not found. Please ensure the payment bill exists and has been analyzed.',
            'error_code': 'EXPENSE_BILL_OR_ANALYSIS_NOT_FOUND'
        }, status=status.HTTP_404_NOT_FOUND)

    if bill.status != TallyPaymentBill.BillStatus.VERIFIED:
        return Response({
            'error': 'Payment Bill Not Ready for Sync',
            'message': f'Payment bill must be in "Verified" status to sync with Tally. Current status: {bill.status}. Please verify the payment bill first.',
            'current_status': bill.status,
            'required_status': 'Verified',
            'error_code': 'EXPENSE_BILL_NOT_VERIFIED'
        }, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

    # Payment voucher needs a Vendor ledger picked before it can be synced.
    if analyzed_bill.vendor is None:
        return Response({
            'error': 'Vendor Not Selected',
            'message': 'This payment has no Vendor ledger picked. Select one before syncing to Tally.',
            'error_code': 'VENDOR_MISSING',
        }, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

    # Payment Mode (the actual Bank/Cash ledger) is required too — the
    # sync XML posts its DEBIT entry using this ledger (Correction 26).
    if analyzed_bill.payment_mode is None:
        return Response({
            'error': 'Payment Mode Not Selected',
            'message': 'This payment has no Payment Mode (Bank/Cash) ledger picked. Select one before syncing to Tally.',
            'error_code': 'PAYMENT_MODE_MISSING',
        }, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

    # Master-sync check is advisory (see vendor_bills.py note).
    from .bill_sync_guard import find_pending_masters
    pending_masters = find_pending_masters(analyzed_bill)

    try:
        sync_data = get_structured_payment_bill_data(analyzed_bill, organization)

        # Reset sync flags on every fresh attempt so the Tally Sync Status
        # modal doesn't show a stale error from the previous try.
        bill.status = TallyPaymentBill.BillStatus.SYNCED
        bill.tally_synced = False
        bill.tally_sync_message = ""
        bill.save(update_fields=['status', 'tally_synced', 'tally_sync_message'])

        tally_state = "confirmed" if bill.tally_synced else "pending_tally"

        try:
            sync_response = payment_bill_sync_external_handler(sync_data, org_id, organization)
            return Response({
                "message": (
                    "Payment bill queued for Tally sync"
                    if tally_state == "pending_tally"
                    else "Payment bill synced to Tally"
                ),
                "bill_id": str(bill_id),
                "status": "Synced",
                "tally_sync_status": tally_state,
                "pending_masters": pending_masters,
                "pending_masters_count": len(pending_masters),
                "sync_data": sync_data,
                "external_sync": sync_response
            }, status=status.HTTP_200_OK)

        except Exception as sync_error:
            logger.warning(f"External payment sync failed but bill status updated: {str(sync_error)}")
            return Response({
                "message": "Payment bill queued for Tally sync (external handler failed)",
                "bill_id": str(bill_id),
                "status": "Synced",
                "tally_sync_status": tally_state,
                "pending_masters": pending_masters,
                "pending_masters_count": len(pending_masters),
                "sync_data": sync_data,
                "external_sync_error": str(sync_error)
            }, status=status.HTTP_200_OK)

    except Exception as e:
        logger.error(f"Payment bill sync failed: {str(e)}")
        return Response({
            'error': 'Payment Bill Sync Failed',
            'message': 'The payment bill sync process encountered an error. This could be due to Tally system connectivity issues or invalid payment data. Please verify your data and try again.',
            'details': str(e),
            'error_code': 'EXPENSE_SYNC_FAILED'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


def payment_bill_sync_external_handler(sync_data, org_id, organization):
    """Handle external sync with the provided payload"""
    try:
        # Log the sync attempt
        logger.info(f"External payment sync handler called for organization {organization.id}")
        logger.info(f"Payment sync data: {json.dumps(sync_data, indent=2)}")

        # Here you can add any external API calls or processing
        # For now, we'll just return a success response
        return {
            "status": "success",
            "message": "Payment payload received and processed",
            "data": sync_data
        }

    except Exception as e:
        logger.error(f"External payment sync handler failed: {str(e)}")
        raise Exception(f"External payment sync failed: {str(e)}")


# ============================================================================
# Delete Payment Bill
# ✅
@extend_schema(
    summary="Delete Payment Bill",
    description="Delete an payment bill and its associated file",
    responses={204: None},
    tags=['Tally Payment Bills']
)
@api_view(['DELETE'])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def payment_bill_delete(request, org_id, bill_id):
    """Delete payment bill."""
    from .bill_helpers import bill_delete_base
    return bill_delete_base(request, org_id, bill_id, TallyPaymentBill)


# ============================================================================
# Get Bills by Status
# ✅
@extend_schema(
    summary="Get Bills by Status",
    description="Get payment bills filtered by status",
    responses={200: TallyPaymentBillSerializer(many=True)},
    tags=['Tally Payment Bills']
)
@api_view(['GET'])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def payment_bills_by_status(request, org_id):
    """Get bills filtered by status."""
    from .bill_helpers import bills_by_status_base
    return bills_by_status_base(request, org_id, TallyPaymentBill, TallyPaymentBillSerializer)


# ============================================================================
# Tally TCP Integration Views
# ✅
@extend_schema(
    summary="Get All Synced Payment Bills",
    description="Get all synced payment bills with their products for the organization",
    responses={200: PaymentBillSyncResponseSerializer(many=True)},
    tags=['Tally TCP']
)
@api_view(['GET'])
@permission_classes([OrganizationAPIKeyOrBearerToken])
def payment_bills_sync_list(request, org_id):
    """Get all synced payment bills with their products"""
    organization = get_organization_from_request(request, org_id)

    if not organization:
        return Response(
            {
                'error': 'Organization Access Denied',
                'message': f'Organization with ID {org_id} not found or you do not have access to it. Please check the organization ID and your API key permissions.',
                'error_code': 'ORG_NOT_FOUND'
            },
            status=status.HTTP_404_NOT_FOUND
        )

    # Get all analyzed bills where the main bill status is "Synced" and tally_synced status is False
    analyzed_bills = TallyPaymentAnalyzedBill.objects.filter(
        organization=organization,
        selected_bill__status=TallyPaymentBill.BillStatus.SYNCED,
        selected_bill__tally_synced=False,
        # Never hand a trashed bill to the Tally TCP bridge.
        selected_bill__is_deleted=False,
    ).select_related(
        'selected_bill', 'vendor', 'payment_mode', 'igst_taxes', 'cgst_taxes', 'sgst_taxes'
    ).prefetch_related(
        'products__chart_of_accounts',
        'consolidated_products__chart_of_accounts',
        'gst_lines__ledger',
    ).order_by('-created_at')

    bills_data = []
    skipped = []
    for analyzed_bill in analyzed_bills:
        try:
            sync_data = prepare_payment_sync_data(analyzed_bill, organization)
        except ValueError as ve:
            # Bill has a missing-COA balancing issue. Skip it — the FE
            # detail page for that bill will surface the same error on
            # its manual sync attempt.
            skipped.append({"bill_no": analyzed_bill.bill_no, "reason": str(ve)})
            logger.warning(
                "Skipping payment bill %s from sync-list: %s",
                analyzed_bill.bill_no, ve,
            )
            continue
        bills_data.append(sync_data["data"])
    if skipped:
        logger.info("Payment sync-list skipped %d bill(s): %s", len(skipped), skipped)

    # Tally TCP/TDL consumes XML natively (see vendor_bills.py for the
    # reasoning). Payment voucher payload has the SAME shape as journal
    # voucher — only ``voucher_type`` value differs ("Payment" instead
    # of "Journal"), so the downstream TCP just needs a Payment-voucher
    # handler routing the same ``<ledgers>`` collection.
    xml_payload = _sync_data_to_xml(bills_data)
    return HttpResponse(xml_payload, content_type='application/xml; charset=utf-8')


def prepare_payment_sync_data(analyzed_bill, organization):
    """Build the sync payload for Tally TCP (Payment voucher).

    Top-level shape (mirrors journal voucher — only ``voucher_type`` value differs):
        {
          "bill_no": str, "bill_date": "DD-MM-YYYY",
          "voucher_type": "Payment",
          "vendor": "", "company": str,
          "total_amount": "54500.00",
          "notes": str,
          "ledgers": [
            # Every posting that hits a Tally ledger, flat list.
            # Each entry: amount, ledger (name), debit_or_credit.
            # GST entries additionally carry "rate" for cross-check.
            {"amount": "54500.00", "ledger": "HDFC Bank",
             "debit_or_credit": "debit"},
            {"amount": "4500.00", "ledger": "CGST (ITC) @ 9%", "rate": "18%",
             "debit_or_credit": "debit"},
            {"amount": "50000.00", "ledger": "Office Rent",
             "debit_or_credit": "credit"},
            ...
          ]
        }

    Client Correction 26: the picked "Vendor" ledger is identification
    only and its name/GST is deliberately NOT sent — ``<vendor>`` is
    always an empty string. The Payment Mode ledger (the actual
    Bank/Cash account) is posted as a single DEBIT entry equal to
    ``analyzed_bill.total``, and every expense/payment line is posted as
    a CREDIT entry — the reverse of a purchase-side voucher, since a
    Payment voucher debits the bank and credits the expense/vendor.

    Zero-amount entries are dropped. ``round_off`` may be negative.
    """
    payment_mode_ledger = analyzed_bill.payment_mode
    bill_date_str = (
        analyzed_bill.bill_date.strftime('%d-%m-%Y') if analyzed_bill.bill_date else None
    )
    company_name = organization.name if hasattr(organization, 'name') else str(organization.id)

    # Base URL is configurable so staging/self-hosted deployments
    # don't leak the production domain into Tally narration.
    _base = getattr(settings, "SITE_URL", "https://billmunshi.com").rstrip("/")
    bill_url = f"{_base}/tally/payment-bill/{analyzed_bill.selected_bill.id}"
    notes_message = f"Payment voucher entered via BillMunshi {bill_url}"

    Q2 = Decimal('0.01')

    def _money(value):
        if value is None or value == '':
            return Decimal('0.00')
        try:
            return Decimal(str(value)).quantize(Q2)
        except (InvalidOperation, ValueError, TypeError):
            return Decimal('0.00')

    def _fmt_money(value):
        return f"{_money(value):.2f}"

    def _dc(value):
        """Normalise DR/CR to lowercase ``debit`` or ``credit``; default debit."""
        v = (str(value) if value else "").strip().lower()
        return v if v in ("debit", "credit") else "debit"

    # ------------------------------------------------------------------
    # Source lines for the payment entries. Pulls from
    # consolidated_products when consolidate=True, individual products
    # otherwise.
    # ------------------------------------------------------------------
    use_consolidated = bool(getattr(analyzed_bill, 'consolidate', False))
    if use_consolidated:
        try:
            source_lines = list(analyzed_bill.consolidated_products.all())
            if not source_lines:
                use_consolidated = False
        except Exception as exc:
            logger.error(
                f"Error reading consolidated payment products for bill "
                f"{analyzed_bill.bill_no}: {exc}"
            )
            use_consolidated = False

    if not use_consolidated:
        source_lines = list(analyzed_bill.products.all())

    logger.info(
        "prepare_payment_sync_data: bill=%s, lines=%d, source=%s",
        analyzed_bill.bill_no, len(source_lines),
        'consolidated' if use_consolidated else 'individual',
    )

    # ------------------------------------------------------------------
    # Ledgers — flat list with DR/CR. Order keeps the payload
    # diff-friendly: vendor → payment items → GST → TDS → other adj → round-off.
    # Each entry is dropped if its amount is zero or its ledger is missing.
    #
    # NOTE: Payment items are emitted INSIDE ``<ledgers>`` too — the
    # journal voucher has no separate ``<items>`` block. Each item just
    # becomes another ``<ledger>`` entry with amount + ledger + DR/CR.
    # The original ``<details>`` text is folded into the bill-level
    # ``<notes>`` (no separate per-line narration sent to Tally).
    # ------------------------------------------------------------------
    ledgers_payload = []

    # Payment Mode — the Bank/Cash ledger the payment is made through.
    # ALWAYS a DEBIT entry for ``analyzed_bill.total`` (Correction 26):
    # the client wants the bank ledger debited and the expense/vendor
    # side credited, which is the reverse of the vendor/purchase voucher.
    # The old "vendor" FK is no longer posted here — it's identification
    # only now (see module docstring / model comment).
    if payment_mode_ledger and _money(analyzed_bill.total) > 0:
        ledgers_payload.append({
            "amount": _fmt_money(analyzed_bill.total),
            "ledger": payment_mode_ledger.name or "Unknown Payment Mode",
            "debit_or_credit": "debit",
        })

    # Payment item lines — booked against the chart-of-accounts ledger.
    # ALWAYS a CREDIT entry (Correction 26) regardless of the line's own
    # stored ``debit_or_credit`` — the Payment Mode debit above is the
    # only debit side of the voucher. A row with a real amount but no
    # COA would silently disappear from the voucher and leave DR/CR out
    # of balance in Tally, so refuse to build a partial payload — the
    # caller must fix the missing COA before sync.
    missing_coa = []
    for line in source_lines:
        amt = _money(getattr(line, 'amount', 0))
        if amt == 0:
            continue
        coa = getattr(line, 'chart_of_accounts', None)
        if not coa:
            missing_coa.append(
                getattr(line, 'item_details', None) or str(getattr(line, 'id', 'unknown'))
            )
            continue
        ledgers_payload.append({
            "amount": _fmt_money(amt),
            "ledger": str(coa),
            "debit_or_credit": "credit",
        })
    if missing_coa:
        raise ValueError(
            "Cannot build payment sync payload — the following line(s) "
            "have an amount but no Chart of Accounts ledger picked, which "
            "would leave the Tally voucher unbalanced: "
            + ", ".join(missing_coa)
        )

    # GST lines — multi-rate support. Each ``TallyPaymentGstLine`` row
    # becomes one ``<ledger>`` entry inside ``<ledgers>``. Mixed-rate
    # bills produce multiple entries of the same tax_type (e.g. two
    # CGST entries at 18% and 28%) — Tally TDL iterates and posts each
    # as its own voucher line.
    #
    # NOTE: replaces the old bill-level cgst/sgst/igst single fields.
    # Backfill migration converted existing data; the legacy fields
    # remain on the model for one release as a safety net but are no
    # longer the source of truth.
    for gst_line in analyzed_bill.gst_lines.all():
        amt = _money(gst_line.amount)
        if amt == 0 or not gst_line.ledger:
            continue
        ledgers_payload.append({
            "amount": _fmt_money(amt),
            "ledger": str(gst_line.ledger),
            "rate": gst_line.rate or "",
            "debit_or_credit": _dc(gst_line.debit_or_credit),
        })

    # TDS, Other Adjustment, Round Off (single bill-level entries each).
    extras = (
        ("tds", analyzed_bill.tds, analyzed_bill.tds_taxes,
         analyzed_bill.tds_debit_or_credit),
        ("other_adjustment", analyzed_bill.other_adjustment,
         analyzed_bill.other_adjustment_taxes,
         analyzed_bill.other_adjustment_debit_or_credit),
        ("round_off", analyzed_bill.round_off, analyzed_bill.round_off_taxes,
         analyzed_bill.round_off_debit_or_credit),
    )
    for _tax_type, amount, ledger, dc in extras:
        amt = _money(amount)
        if amt == 0 or not ledger:
            continue
        ledgers_payload.append({
            "amount": _fmt_money(amt),
            "ledger": str(ledger),
            "debit_or_credit": _dc(dc),
        })

    bill_data = {
        # ``id`` is the BillMunshi bill UUID. The Tally TCP / TDL connector
        # echoes it back via the ``tally_status`` callback API so the
        # backend can flag the right bill as posted (``tally_synced=True``)
        # without having to match on bill_no + vendor + date heuristics.
        "id": str(analyzed_bill.selected_bill.id),
        "bill_no": analyzed_bill.bill_no or "",
        "bill_date": bill_date_str,
        "voucher_type": "Payment",
        # Client Correction 26: never leak the picked vendor's name/GST
        # into the sync XML — the tag is always emitted empty.
        "vendor": "",
        "company": company_name,
        "total_amount": _fmt_money(analyzed_bill.total),
        "notes": notes_message,
        "ledgers": ledgers_payload,
        # NOTE: No ``items`` key — payment item lines are folded into
        # ``ledgers`` above (see comment block). The Tally Journal voucher
        # has only one collection to iterate.
    }

    return {"data": bill_data}


@extend_schema(
    summary="Sync Payment Bill to External System",
    description="Accept payment bill data payload for external system sync",
    responses={
        200: OpenApiResponse(description="Payload accepted successfully"),
        400: OpenApiResponse(description="Invalid payload")
    },
    tags=['Tally TCP']
)
@api_view(['POST'])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def payment_bill_sync_external(request, org_id):
    """Accept payment bill payload for external system sync"""
    organization = get_organization_from_request(request, org_id)
    if not organization:
        return Response(
            {
                'error': 'Organization Access Denied',
                'message': f'Organization with ID {org_id} not found or you do not have access to it. Please check the organization ID and your permissions.',
                'error_code': 'ORG_NOT_FOUND'
            },
            status=status.HTTP_404_NOT_FOUND
        )

    # Get the payload from request data
    payload = request.data

    if not payload:
        return Response(
            {
                'error': 'Missing Payload',
                'message': 'No payload data provided for external payment sync. Please provide the payment bill data to be processed.',
                'error_code': 'NO_EXPENSE_PAYLOAD_PROVIDED'
            },
            status=status.HTTP_422_UNPROCESSABLE_ENTITY
        )

    try:
        # Log the received payload
        logger.info(f"External payment sync received payload for organization {organization.id}")
        logger.info(f"Payment Payload: {json.dumps(payload, indent=2)}")

        # Here you can process the payload as needed
        # For now, we'll just acknowledge receipt

        return Response({
            'message': 'Payment payload received and processed successfully',
            'organization_id': str(organization.id),
            'payload_received': True,
            'timestamp': datetime.now().isoformat()
        }, status=status.HTTP_200_OK)

    except Exception as e:
        logger.error(f"External payment sync failed: {str(e)}")
        return Response({
            'error': 'External Payment Sync Processing Failed',
            'message': 'There was an error processing the external payment sync payload. This could be due to invalid data format or system issues.',
            'details': str(e),
            'error_code': 'EXTERNAL_EXPENSE_SYNC_FAILED'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

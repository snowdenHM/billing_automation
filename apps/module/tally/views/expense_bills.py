# apps/module/tally/expense_views_functional.py

import json
import logging
import os
import random
from datetime import datetime
from decimal import Decimal
from io import BytesIO

from PyPDF2 import PdfReader
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
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
    TallyExpenseBill,
    TallyExpenseAnalyzedBill,
    TallyExpenseAnalyzedProduct,
    TallyExpenseConsolidatedProduct,
    Ledger,
    ParentLedger,
    TallyConfig,
    TallyVendorBill
)
from ..serializers import (
    TallyExpenseBillSerializer,
    TallyExpenseAnalyzedBillSerializer,
    ExpenseBillUploadSerializer,
    ExpenseBillAnalysisRequestSerializer,
    ExpenseBillVerificationSerializer,
    ExpenseBillSyncRequestSerializer,
    ExpenseBillSyncResponseSerializer
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
parse_expense_bill_date = parse_bill_date


def check_duplicate_tally_expense_bill(bill, organization):
    """Wrapper: delegates to apps.common.services.duplicate_detection.check_duplicate_bill."""
    return check_duplicate_bill(bill, organization, TallyExpenseBill)


def analyze_expense_bill_with_ai(bill, organization):
    """Analyze expense bill using OpenAI API — delegates to shared service."""
    logger.info(f"Starting AI analysis for expense bill {bill.id}, file: {bill.file.name}")

    try:
        json_data = analyze_bill_file(bill.file.path, bill.file.name, get_expense_bill_prompt())
        logger.info(f"AI analysis successful for expense bill {bill.id}")
    except Exception as e:
        logger.error(f"AI analysis failed for expense bill {bill.id}: {e}")
        raise Exception(f"AI processing failed: {str(e)}")

    # Process and save extracted data
    return process_expense_analysis_data(bill, json_data, organization)


def validate_expense_bill_ownership(json_data, organization):
    """Validate if the expense bill belongs to the organization — delegates to shared service.

    Checks the ``to`` field (customer/recipient). Lenient when no customer info
    is present (common for general receipts).
    """
    from apps.common.services.ownership import validate_bill_ownership_simple
    return validate_bill_ownership_simple(
        json_data, organization, check_field='to', allow_empty=True,
        bill_type='expense bill',
    )


def process_expense_analysis_data(bill, json_data, organization):
    """Process AI extracted data and create analyzed expense bill with automatic vendor and tax selection"""
    try:
        # 🛡️ STEP 1: Validate bill ownership
        ownership_valid, ownership_message = validate_expense_bill_ownership(json_data, organization)
        if not ownership_valid:
            logger.warning(f"⚠️ Expense bill {bill.id} ownership validation failed: {ownership_message}")
            # Store ownership validation results only if fields exist
            if hasattr(bill.__class__, 'ownership_validation_status'):
                bill.ownership_validation_status = 'failed'
            if hasattr(bill.__class__, 'ownership_validation_message'):
                bill.ownership_validation_message = ownership_message
        else:
            logger.info(f"✅ Expense bill {bill.id} ownership validation passed: {ownership_message}")
            # Store ownership validation results only if fields exist
            if hasattr(bill.__class__, 'ownership_validation_status'):
                bill.ownership_validation_status = 'passed'
            if hasattr(bill.__class__, 'ownership_validation_message'):
                bill.ownership_validation_message = ownership_message
            
        logger.info(f"Processing expense analysis data for bill {bill.id} with automation")
            
        # Log the raw JSON data for debugging
        logger.info(f"Raw JSON data from OpenAI: {json.dumps(json_data, indent=2)}")

        # Extract relevant data with robust error handling
        relevant_data = {}

        # Handle different JSON response formats from OpenAI
        if isinstance(json_data, dict):
            if "properties" in json_data:
                # Handle schema format - extract from properties with safe access
                try:
                    relevant_data = {
                        "billNumber": safe_get_nested(json_data, ["properties", "billNumber", "const"], ""),
                        "dateIssued": safe_get_nested(json_data, ["properties", "dateIssued", "const"], ""),
                        "from": safe_get_nested(json_data, ["properties", "from", "properties"], {}),
                        "to": safe_get_nested(json_data, ["properties", "to", "properties"], {}),
                        "expenses": extract_expenses_from_properties(json_data),
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

        # Save analyzed data to bill
        bill.analysed_data = relevant_data
        bill.save(update_fields=['analysed_data'])

        # Extract required fields with safe access
        bill_number = str(relevant_data.get('billNumber', '')).strip()
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
        bill_date = parse_expense_bill_date(date_issued)
        
        # Parse due date if provided  
        due_date_issued = str(relevant_data.get('dueDate', ''))
        due_date = parse_expense_bill_date(due_date_issued) if due_date_issued else None

        # Extract financial information with GST details
        igst_val = _to_decimal(relevant_data.get('igst', 0))
        cgst_val = _to_decimal(relevant_data.get('cgst', 0))  
        sgst_val = _to_decimal(relevant_data.get('sgst', 0))
        tds_val = _to_decimal(relevant_data.get('tds', 0))
        total_val = _to_decimal(relevant_data.get('total', 0))

        logger.info(f"Extracted expense data - Bill: {bill_number}, Vendor: {company_name}, GST: {vendor_gst_number}, Total: ₹{total_val}, IGST: {igst_val}, CGST: {cgst_val}, SGST: {sgst_val}")
        
        # 🎯 STEP 2: Automatic vendor ledger finding with GST matching  
        vendor_ledger = find_expense_vendor_ledger(
            company_name=company_name, 
            organization=organization, 
            vendor_gst=vendor_gst_number
        )
        if vendor_ledger:
            logger.info(f"✅ Automatically selected vendor ledger: {vendor_ledger.name} (ID: {vendor_ledger.id})")
        else:
            logger.warning(f"⚠️ No vendor ledger found for: {company_name} (GST: {vendor_gst_number})")

        # Determine GST type with safe conversion and proper decimal rounding
        if igst_val > 0:
            gst_type = TallyExpenseAnalyzedBill.GSTType.IGST
        elif cgst_val > 0 or sgst_val > 0:
            gst_type = TallyExpenseAnalyzedBill.GSTType.CGST_SGST
        else:
            gst_type = TallyExpenseAnalyzedBill.GSTType.UNKNOWN

        # 🎯 STEP 3: Automatic tax ledger finding for bill-level GST values
        logger.info(f"[NEW EXPENSE] Finding bill-level tax ledgers - IGST: {igst_val}, CGST: {cgst_val}, SGST: {sgst_val}")
        
        igst_tax_ledger = find_appropriate_expense_tax_ledger(organization, float(igst_val), 0, 0, ledger_type='bill') if igst_val > 0 else None
        cgst_tax_ledger = find_appropriate_expense_tax_ledger(organization, 0, float(cgst_val), 0, ledger_type='bill') if cgst_val > 0 else None
        sgst_tax_ledger = find_appropriate_expense_tax_ledger(organization, 0, 0, float(sgst_val), ledger_type='bill') if sgst_val > 0 else None

        if igst_tax_ledger:
            logger.info(f"✅ Automatically selected IGST ledger: {igst_tax_ledger.name} (Amount: ₹{igst_val})")
        if cgst_tax_ledger:
            logger.info(f"✅ Automatically selected CGST ledger: {cgst_tax_ledger.name} (Amount: ₹{cgst_val})")
        if sgst_tax_ledger:
            logger.info(f"✅ Automatically selected SGST ledger: {sgst_tax_ledger.name} (Amount: ₹{sgst_val})")

        # 🎯 STEP 4: Find expense COA ledger for line items
        expense_coa_ledger = find_appropriate_expense_tax_ledger(organization, 0, 0, 0, ledger_type='expense_coa')
        if expense_coa_ledger:
            logger.info(f"✅ Automatically selected Expense COA ledger: {expense_coa_ledger.name}")

        # Create analyzed bill with automatic selections
        with transaction.atomic():
            analyzed_bill = TallyExpenseAnalyzedBill.objects.create(
                selected_bill=bill,
                vendor=vendor_ledger,  # Automatically selected
                bill_no=bill_number,
                bill_date=bill_date,
                due_date=due_date,
                igst=igst_val,
                cgst=cgst_val,
                sgst=sgst_val,
                tds=tds_val,
                total=total_val,
                igst_taxes=igst_tax_ledger,  # Automatically selected
                cgst_taxes=cgst_tax_ledger,  # Automatically selected
                sgst_taxes=sgst_tax_ledger,  # Automatically selected
                note="AI Analyzed Expense Bill with Automation",
                organization=organization,
                gst_type=gst_type,
                igst_debit_or_credit='debit',
                cgst_debit_or_credit='debit', 
                sgst_debit_or_credit='debit',
                tds_debit_or_credit='credit'
            )

            logger.info(f"✅ Created TallyExpenseAnalyzedBill with ID: {analyzed_bill.id} (with automatic vendor/tax selections)")

            # Create analyzed products (expense items) with auto-assigned COA ledger
            product_instances = []
            expenses = relevant_data.get('expenses', [])
            if isinstance(expenses, list):
                for expense in expenses:
                    if isinstance(expense, dict):
                        # Find specific COA ledger for this expense category
                        expense_category = str(expense.get('category', 'General Expenses'))
                        item_coa_ledger = find_or_create_expense_chart_of_accounts_ledger(expense_category, organization)
                        
                        product = TallyExpenseAnalyzedProduct(
                            expense_bill=analyzed_bill,
                            item_details=str(expense.get('description', '')),
                            chart_of_accounts=item_coa_ledger or expense_coa_ledger,  # Use specific or fallback COA ledger
                            amount=_to_decimal(expense.get('amount', 0)),
                            debit_or_credit=TallyExpenseAnalyzedProduct.DebitCredit.DEBIT,
                            organization=organization
                        )
                        product_instances.append(product)
                        
                        if item_coa_ledger:
                            logger.info(f"✅ Automatically selected COA ledger for '{expense_category}': {item_coa_ledger.name}")

            if product_instances:
                TallyExpenseAnalyzedProduct.objects.bulk_create(product_instances)
                logger.info(f"Successfully created {len(product_instances)} expense products with automatic COA assignments")

                # ✅ CREATE CONSOLIDATED PRODUCTS FOR LAYOUT SWITCHING SUPPORT
                if len(product_instances) > 1:
                    try:
                        total_amount = sum(p.amount for p in product_instances)
                        items_count = len(product_instances)

                        item_details = []
                        for product in product_instances:
                            item_details.append(f'• {product.item_details} (Amount: ₹{product.amount})')

                        consolidated_details = f'Consolidated {items_count} expense entries:\n' + '\n'.join(item_details)

                        consolidated_product = TallyExpenseConsolidatedProduct.objects.create(
                            expense_bill=analyzed_bill,
                            organization=organization,
                            item_details=consolidated_details,
                            chart_of_accounts=expense_coa_ledger,  # Use fallback COA ledger for consolidated
                            amount=total_amount,
                            debit_or_credit=TallyExpenseConsolidatedProduct.DebitCredit.DEBIT,
                            original_entries_count=items_count,
                            consolidation_notes=f'Auto-created with automation for {items_count} expense entries'
                        )

                        logger.info(f"✅ Created consolidated expense product for bill {analyzed_bill.id} with {items_count} entries (₹{total_amount})")

                    except Exception as e:
                        logger.error(f"❌ Error creating consolidated expense product for bill {analyzed_bill.id}: {str(e)}")
                else:
                    logger.info(f"ℹ️ Skipping consolidated expense product creation - bill has only {len(product_instances)} item(s)")

            # Update bill status and save ownership validation results
            bill.status = TallyExpenseBill.BillStatus.ANALYSED
            bill.process = True
            # Update fields list based on what exists in the model
            fields_to_update = ['status', 'process']
            if hasattr(bill, 'ownership_validation_status'):
                fields_to_update.append('ownership_validation_status')
            if hasattr(bill, 'ownership_validation_message'):
                fields_to_update.append('ownership_validation_message')
            bill.save(update_fields=fields_to_update)

            # Check for duplicates
            duplicate_result = check_duplicate_tally_expense_bill(bill, organization)
            if duplicate_result:
                is_duplicate, duplicate_bills, similarity_score = duplicate_result
                if is_duplicate:
                    logger.warning(f"⚠️ Potential duplicate expense bill detected with similarity {similarity_score}")
                    
            logger.info(f"✅ Successfully processed expense analysis for bill {bill.id} with full automation")

            return analyzed_bill

    except Exception as e:
        logger.error(f"❌ Error processing expense analysis data: {str(e)} - Data: {json_data}")
        # Update bill status to failed
        bill.status = TallyExpenseBill.BillStatus.FAILED
        fields_to_update = ['status']
        
        # Only update ownership validation fields if they exist in the model
        if hasattr(bill, 'ownership_validation_status'):
            bill.ownership_validation_status = 'error'
            fields_to_update.append('ownership_validation_status')
        
        if hasattr(bill, 'ownership_validation_message'):
            bill.ownership_validation_message = f"Processing failed: {str(e)}"
            fields_to_update.append('ownership_validation_message')
        
        bill.save(update_fields=fields_to_update)
        raise Exception(f"Error processing expense analysis data: {str(e)}")


def extract_expenses_from_properties(json_data):
    """Safely extract expenses from properties format"""
    try:
        expenses_data = safe_get_nested(json_data, ["properties", "expenses", "items"], [])
        if isinstance(expenses_data, list):
            extracted_expenses = []
            for expense in expenses_data:
                if isinstance(expense, dict):
                    extracted_expense = {
                        "description": safe_get_nested(expense, ["description", "const"], ""),
                        "category": safe_get_nested(expense, ["category", "const"], ""),
                        "amount": safe_get_nested(expense, ["amount", "const"], 0)
                    }
                    extracted_expenses.append(extracted_expense)
            return extracted_expenses
        return []
    except Exception:
        return []


def find_appropriate_expense_tax_ledger(organization, igst_val, cgst_val, sgst_val, ledger_type='bill'):
    """Delegates to shared find_tally_tax_ledger in helpers."""
    from .helpers import find_tally_tax_ledger
    return find_tally_tax_ledger(organization, igst_val, cgst_val, sgst_val, ledger_type=ledger_type)


def find_expense_vendor_ledger(company_name, organization, vendor_gst=None):
    """Delegates to shared find_tally_vendor_ledger in helpers."""
    from .helpers import find_tally_vendor_ledger
    return find_tally_vendor_ledger(
        company_name, organization, vendor_gst=vendor_gst,
        use_config_threshold=True, gst_search_scope='all',
        fallback_all_ledgers=True
    )


def process_pdf_splitting_expense(pdf_file, organization, file_type, uploaded_by):
    """Split PDF into individual pages and create separate expense bills"""
    return split_pdf_to_bills(
        pdf_file=pdf_file,
        organization=organization,
        file_type=file_type,
        uploaded_by=uploaded_by,
        bill_model=TallyExpenseBill,
        filename_prefix="BM-Expense-Page"
    )


# ============================================================================
# API Views
# ✅
@extend_schema(
    summary="List Expense Bills",
    description="Get all expense bills for the organization",
    responses={200: TallyExpenseBillSerializer(many=True)},
    tags=['Tally Expense Bills']
)
@api_view(['GET'])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def expense_bills_list(request, org_id):
    """Get all expense bills for the organization."""
    from .bill_helpers import bills_list_base
    return bills_list_base(
        request, org_id,
        bill_model=TallyExpenseBill,
        serializer_class=TallyExpenseBillSerializer,
        include_ownership_filter=False,  # Expense doesn't have ownership filter
    )


# ✅
@extend_schema(
    summary="Upload Expense Bills",
    description="Upload single or multiple expense bill files (PDF, JPG, PNG). Supports both single file and multiple file uploads.",
    request=ExpenseBillUploadSerializer,
    responses={201: TallyExpenseBillSerializer(many=True)},
    tags=['Tally Expense Bills']
)
@api_view(['POST'])
@permission_classes([IsAuthenticated, IsOrgAdmin])
@parser_classes([MultiPartParser, FormParser])
def expense_bills_upload(request, org_id):
    """Handle single or multiple expense bill file uploads with PDF splitting support"""
    from .bill_helpers import bills_upload_base
    from ..tasks import enqueue_expense_bill_processing
    
    return bills_upload_base(
        request=request,
        org_id=org_id,
        bill_model=TallyExpenseBill,
        upload_serializer_class=ExpenseBillUploadSerializer,
        response_serializer_class=TallyExpenseBillSerializer,
        pdf_split_func=process_pdf_splitting_expense,
        enqueue_func=enqueue_expense_bill_processing,
        bill_type_label="expense"
    )


# ✅
@extend_schema(
    summary="Check Bill Processing Status",
    description="Check the status of background processing for an expense bill",
    responses={200: "Bill processing status information"},
    tags=['Tally Expense Bills']
)
@api_view(['GET'])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def expense_bill_processing_status(request, org_id, bill_id):
    """Check the processing status of an expense bill"""
    from .bill_helpers import bill_processing_status_base
    return bill_processing_status_base(request, org_id, bill_id, TallyExpenseBill, "Expense")


# ✅
@extend_schema(
    summary="Analyze Expense Bill",
    description="Analyze expense bill using OpenAI to extract expense data",
    request=ExpenseBillAnalysisRequestSerializer,
    responses={
        200: TallyExpenseAnalyzedBillSerializer,
        400: OpenApiResponse(description="Analysis failed")
    },
    tags=['Tally Expense Bills']
)
@api_view(['POST'])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def expense_bill_analyze(request, org_id):
    """Analyze expense bill using OpenAI"""
    serializer = ExpenseBillAnalysisRequestSerializer(data=request.data)
    if not serializer.is_valid():
        return Response({
            'error': 'Invalid Analysis Request',
            'message': 'The expense bill analysis request data is invalid. Please ensure the bill_id is provided and valid.',
            'details': serializer.errors,
            'error_code': 'INVALID_ANALYSIS_REQUEST'
        }, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

    bill_id = serializer.validated_data['bill_id']
    organization = get_organization_from_request(request, org_id)

    try:
        bill = TallyExpenseBill.objects.get(
            id=bill_id,
            organization=organization
        )
    except TallyExpenseBill.DoesNotExist:
        return Response(
            {'error': 'Bill not found'},
            status=status.HTTP_404_NOT_FOUND
        )

    if bill.process:
        return Response({
            'message': 'Expense Bill Already Analyzed',
            'data': 'This expense bill has already been processed and analyzed. Use the verification endpoint to modify the analyzed data.',
            'error_code': 'EXPENSE_BILL_ALREADY_PROCESSED'
        }, status=status.HTTP_200_OK)

    try:
        # Check if bill already has analyzed data
        if bill.analysed_data:
            logger.info(f"Using existing analyzed data for expense bill {bill_id}")
            analyzed_bill = process_existing_expense_analysis_data(bill, bill.analysed_data, organization)
        else:
            logger.info(f"Running new OpenAI analysis for expense bill {bill_id}")
            analysis_result = analyze_expense_bill_with_ai(bill, organization)
            if isinstance(analysis_result, dict) and not analysis_result.get('success'):
                return Response({
                    'error': 'Expense Bill Analysis Failed',
                    'message': 'The expense bill analysis could not be completed. This might be due to poor image quality, unsupported file format, or AI service issues.',
                    'details': analysis_result.get('error', 'Unknown error'),
                    'error_code': 'EXPENSE_ANALYSIS_FAILED'
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            analyzed_bill = analysis_result if not isinstance(analysis_result, dict) else analysis_result.get('analyzed_bill')

        # Check for duplicate bills after analysis
        is_duplicate, duplicate_bills, max_similarity = check_duplicate_tally_expense_bill(bill, organization)

        response_data = {
            "detail": "Tally expense bill analyzed successfully",
            "analyzed_bill": TallyExpenseAnalyzedBillSerializer(analyzed_bill).data
        }

        # Add duplicate warnings if found
        if is_duplicate:
            duplicate_warnings = []
            for dup in duplicate_bills:
                duplicate_bill = dup['bill']
                # Build bill URL
                bill_url = None
                if duplicate_bill.file:
                    try:
                        bill_url = request.build_absolute_uri(duplicate_bill.file.url)
                    except Exception:
                        bill_url = duplicate_bill.file.url

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
                "warning_message": f"⚠️ DUPLICATE DETECTED: Found {len(duplicate_bills)} similar Tally expense bill(s) in your organization. "
                                  f"This bill appears to be {round(max_similarity, 1)}% similar to existing bills. "
                                  "Please review carefully before proceeding to avoid duplicate entries."
            })

            logger.warning(f"Duplicate Tally expense bill detected for {bill.bill_munshi_name} - {len(duplicate_bills)} similar bills found")

        return Response(response_data, status=status.HTTP_200_OK)

    except Exception as e:
        logger.error(f"Expense bill analysis failed: {str(e)}")
        return Response({
            'error': 'Expense Bill Analysis Failed',
            'message': 'The expense bill analysis could not be completed. This might be due to poor image quality, unsupported file format, or AI service issues.',
            'details': str(e),
            'error_code': 'EXPENSE_ANALYSIS_FAILED'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


def process_existing_expense_analysis_data(bill, existing_data, organization):
    """Process existing analyzed data without calling OpenAI again"""
    try:
        logger.info(f"Processing existing analyzed data for expense bill {bill.id}")

        # Check if analyzed bill already exists
        try:
            analyzed_bill = TallyExpenseAnalyzedBill.objects.get(selected_bill=bill)
            logger.info(f"Found existing analyzed expense bill {analyzed_bill.id}")
            return analyzed_bill
        except TallyExpenseAnalyzedBill.DoesNotExist:
            pass

        # Extract required fields with safe access
        bill_number = str(existing_data.get('billNumber', '')).strip()
        date_issued = str(existing_data.get('dateIssued', ''))

        # Handle 'from' field safely
        from_data = existing_data.get('from', {})
        if isinstance(from_data, dict):
            company_name = str(from_data.get('name', '')).strip().lower()
        else:
            company_name = str(from_data).strip().lower()

        # Parse date with multiple format support
        bill_date = parse_expense_bill_date(date_issued)

        # Find vendor ledger
        vendor = find_expense_vendor_ledger(company_name, organization)

        # Determine GST type with safe conversion and proper decimal rounding
        igst_val = _to_decimal(existing_data.get('igst', 0))
        cgst_val = _to_decimal(existing_data.get('cgst', 0))
        sgst_val = _to_decimal(existing_data.get('sgst', 0))
        tds_val = _to_decimal(existing_data.get('tds', 0))
        total_val = _to_decimal(existing_data.get('total', 0))

        if igst_val > 0:
            gst_type = TallyExpenseAnalyzedBill.GSTType.IGST
        elif cgst_val > 0 or sgst_val > 0:
            gst_type = TallyExpenseAnalyzedBill.GSTType.CGST_SGST
        else:
            gst_type = TallyExpenseAnalyzedBill.GSTType.UNKNOWN

        # 🏛️ FIND APPROPRIATE TAX LEDGERS FOR BILL-LEVEL GST VALUES
        logger.info(f"[EXISTING EXPENSE] Finding bill-level tax ledgers - IGST: {igst_val}, CGST: {cgst_val}, SGST: {sgst_val}")
        
        igst_tax_ledger = None
        cgst_tax_ledger = None
        sgst_tax_ledger = None
        
        if igst_val > 0:
            igst_tax_ledger = find_appropriate_expense_tax_ledger(organization, float(igst_val), 0, 0, ledger_type='bill')
        
        if cgst_val > 0:
            cgst_tax_ledger = find_appropriate_expense_tax_ledger(organization, 0, float(cgst_val), 0, ledger_type='bill')
        
        if sgst_val > 0:
            sgst_tax_ledger = find_appropriate_expense_tax_ledger(organization, 0, 0, float(sgst_val), ledger_type='bill')

        logger.info(f"[EXISTING EXPENSE] Tax ledgers found - IGST: {igst_tax_ledger}, CGST: {cgst_tax_ledger}, SGST: {sgst_tax_ledger}")

        # 🏛️ FIND EXPENSE COA LEDGER FOR LINE ITEMS
        expense_coa_ledger = find_appropriate_expense_tax_ledger(organization, 0, 0, 0, ledger_type='expense_coa')
        logger.info(f"[EXISTING EXPENSE] Expense COA ledger: {expense_coa_ledger}")

        # Create analyzed bill without Django validation
        with transaction.atomic():
            analyzed_bill = TallyExpenseAnalyzedBill(
                selected_bill=bill,
                vendor=vendor,
                bill_no=bill_number,
                bill_date=bill_date,
                igst=igst_val,
                cgst=cgst_val,
                sgst=sgst_val,
                tds=tds_val,
                total=total_val,
                igst_taxes=igst_tax_ledger,
                cgst_taxes=cgst_tax_ledger,
                sgst_taxes=sgst_tax_ledger,
                note="AI Analyzed Expense Bill (Existing Data)",
                organization=organization,
                gst_type=gst_type
            )

            # Save without calling clean() to skip validation
            analyzed_bill.save(skip_validation=True)

            # Create analyzed products (expense items) with auto-assigned COA ledger
            created_products = []
            expenses = existing_data.get('expenses', [])

            if isinstance(expenses, list):
                for expense in expenses:
                    if isinstance(expense, dict):
                        amount = _to_decimal(expense.get('amount', 0))

                        product = TallyExpenseAnalyzedProduct(
                            expense_bill=analyzed_bill,
                            item_details=str(expense.get('description', '')),
                            chart_of_accounts=expense_coa_ledger,
                            amount=amount,
                            debit_or_credit=TallyExpenseAnalyzedProduct.DebitCredit.DEBIT,
                            organization=organization
                        )
                        created_products.append(product)

            # Bulk create products
            if created_products:
                TallyExpenseAnalyzedProduct.objects.bulk_create(created_products)
                logger.info(f"Successfully created {len(created_products)} expense products for bill {analyzed_bill.id}")

                # ✅ CREATE CONSOLIDATED PRODUCTS FOR LAYOUT SWITCHING SUPPORT
                if len(created_products) > 1:
                    try:
                        total_amount = sum(p.amount for p in created_products)
                        items_count = len(created_products)

                        item_details = []
                        for product in created_products:
                            item_details.append(f'• {product.item_details} (Amount: ₹{product.amount})')

                        consolidated_details = f'Consolidated {items_count} expense entries:\n' + '\n'.join(item_details)

                        consolidated_product = TallyExpenseConsolidatedProduct.objects.create(
                            expense_bill=analyzed_bill,
                            organization=organization,
                            item_details=consolidated_details,
                            chart_of_accounts=expense_coa_ledger,
                            amount=total_amount,
                            debit_or_credit=TallyExpenseConsolidatedProduct.DebitCredit.DEBIT,
                            original_entries_count=items_count,
                            consolidation_notes=f'Auto-created during existing data processing for {items_count} expense entries'
                        )

                        logger.info(f"✅ Created consolidated expense product for bill {analyzed_bill.id} with {items_count} entries (₹{total_amount})")

                    except Exception as e:
                        logger.error(f"❌ Error creating consolidated expense product for bill {analyzed_bill.id}: {str(e)}")
                else:
                    logger.info(f"ℹ️ Bill has only {len(created_products)} item - no consolidation needed")

            # Update bill status
            bill.status = TallyExpenseBill.BillStatus.ANALYSED
            bill.process = True
            bill.save(update_fields=['status', 'process'])

            logger.info(f"Successfully processed existing expense analysis data for bill {bill.id}")
            return analyzed_bill

    except Exception as e:
        logger.error(f"Error processing existing expense analysis data: {str(e)}")
        raise Exception(f"Error processing existing expense analysis data: {str(e)}")


# ============================================================================
# Get Expense Bill Detail
# ✅
@extend_schema(
    summary="Get Expense Bill Detail",
    description="Get detailed information about a specific expense bill including analysis data",
    responses={200: "TallyExpenseBillDetailSerializer"},
    tags=['Tally Expense Bills']
)
@api_view(['GET'])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def expense_bill_detail(request, org_id, bill_id):
    """Get expense bill detail including analysis data using proper serializer"""
    from ..serializers import TallyExpenseBillDetailSerializer
    from .bill_helpers import bill_detail_base
    return bill_detail_base(request, org_id, bill_id, TallyExpenseBill, TallyExpenseBillDetailSerializer)


# ===========================================================================
# Expense Bill Verify View
# ✅
@extend_schema(
    summary="Verify Expense Bill",
    description="Verify analyzed expense bill data and mark as verified",
    request=ExpenseBillVerificationSerializer,
    responses={200: TallyExpenseAnalyzedBillSerializer},
    tags=['Tally Expense Bills']
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def expense_bill_verify(request, org_id):
    """Verify analyzed expense bill with user modifications"""
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
                'message': 'Both bill_id and analyzed_bill parameters are required for expense bill verification. Please provide both values.',
                'error_code': 'REQUIRED_PARAMS_MISSING'
            },
            status=status.HTTP_422_UNPROCESSABLE_ENTITY
        )

    try:
        bill = TallyExpenseBill.objects.get(id=bill_id, organization=organization)
        analyzed_bill = TallyExpenseAnalyzedBill.objects.get(id=analyzed_bill_id, organization=organization)
    except (TallyExpenseBill.DoesNotExist, TallyExpenseAnalyzedBill.DoesNotExist):
        return Response(
            {
                'error': 'Expense Bill or Analysis Data Not Found',
                'message': f'Expense bill with ID {bill_id} or its analyzed data not found. Please ensure the expense bill exists and has been analyzed.',
                'error_code': 'EXPENSE_BILL_OR_ANALYSIS_NOT_FOUND'
            },
            status=status.HTTP_404_NOT_FOUND
        )

    if bill.status not in [TallyExpenseBill.BillStatus.ANALYSED, TallyExpenseBill.BillStatus.VERIFIED]:
        return Response(
            {
                'error': 'Invalid Expense Bill Status',
                'message': f'Expense bill must be in "Analysed" or "Verified" status to perform verification. Current status: {bill.status}',
                'current_status': bill.status,
                'required_status': ['Analysed', 'Verified'],
                'error_code': 'INVALID_EXPENSE_BILL_STATUS'
            },
            status=status.HTTP_422_UNPROCESSABLE_ENTITY
        )

    try:
        # Update the analyzed bill with user modifications
        verified_bill = update_analyzed_expense_bill_data(analyzed_bill, analyzed_data, organization)

        # Update bill status to verified
        bill.status = TallyExpenseBill.BillStatus.VERIFIED
        bill.save(update_fields=['status'])

        # Return the updated data in the same structured format
        response_data = get_structured_expense_bill_data(verified_bill, organization)

        return Response({
            "message": "Expense bill verified successfully",
            "analyzed_data": response_data
        }, status=status.HTTP_200_OK)

    except Exception as e:
        logger.error(f"Expense bill verification failed: {str(e)}")
        return Response({
            'error': 'Expense Bill Verification Failed',
            'message': 'The expense bill verification process encountered an error. This could be due to invalid data or debit/credit imbalance. Please check your entries and try again.',
            'details': str(e),
            'error_code': 'EXPENSE_VERIFICATION_FAILED'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


def update_analyzed_expense_bill_data(analyzed_bill, analyzed_data, organization):
    """Update analyzed expense bill with user modifications"""

    if not analyzed_data:
        return analyzed_bill
    
    # Add defensive check to ensure analyzed_bill is the correct type
    from ..models import TallyExpenseAnalyzedBill
    if not isinstance(analyzed_bill, TallyExpenseAnalyzedBill):
        logger.error(f"Expected TallyExpenseAnalyzedBill, got {type(analyzed_bill)}")
        raise ValueError(f"Invalid analyzed_bill type: {type(analyzed_bill)}")

    with transaction.atomic():
        # Update vendor information - handle flattened structure
        vendor_name = analyzed_data.get('name')
        if vendor_name and vendor_name != "No Ledger":
            # Try to find existing vendor or create if needed
            vendor = find_or_create_expense_vendor_ledger(vendor_name, {}, organization)
            if vendor:
                analyzed_bill.vendor = vendor

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
            bill_date = parse_expense_bill_date(analyzed_data['bill_date'])
            if bill_date:
                analyzed_bill.bill_date = bill_date
        if 'due_date' in analyzed_data:
            # Parse due date string (format: "31-12-2023")
            due_date = parse_expense_bill_date(analyzed_data['due_date'])
            if due_date:
                analyzed_bill.due_date = due_date
        if 'total' in analyzed_data:
            analyzed_bill.total = _to_decimal(analyzed_data['total'])

        # Update tax information
        taxes_data = analyzed_data.get('taxes', {})
        if taxes_data:
            # Update tax amounts with proper decimal conversion
            igst_data = taxes_data.get('igst', {})
            if 'amount' in igst_data:
                analyzed_bill.igst = _to_decimal(igst_data['amount'])
            if 'ledger' in igst_data and igst_data['ledger'] != "No Tax Ledger":
                igst_ledger = find_or_create_expense_tax_ledger(igst_data['ledger'], 'IGST', organization)
                if igst_ledger:
                    analyzed_bill.igst_taxes = igst_ledger
            if 'debit_or_credit' in igst_data:
                analyzed_bill.igst_debit_or_credit = igst_data['debit_or_credit']

            cgst_data = taxes_data.get('cgst', {})
            if 'amount' in cgst_data:
                analyzed_bill.cgst = _to_decimal(cgst_data['amount'])
            if 'ledger' in cgst_data and cgst_data['ledger'] != "No Tax Ledger":
                cgst_ledger = find_or_create_expense_tax_ledger(cgst_data['ledger'], 'CGST', organization)
                if cgst_ledger:
                    analyzed_bill.cgst_taxes = cgst_ledger
            if 'debit_or_credit' in cgst_data:
                analyzed_bill.cgst_debit_or_credit = cgst_data['debit_or_credit']

            sgst_data = taxes_data.get('sgst', {})
            if 'amount' in sgst_data:
                analyzed_bill.sgst = _to_decimal(sgst_data['amount'])
            if 'ledger' in sgst_data and sgst_data['ledger'] != "No Tax Ledger":
                sgst_ledger = find_or_create_expense_tax_ledger(sgst_data['ledger'], 'SGST', organization)
                if sgst_ledger:
                    analyzed_bill.sgst_taxes = sgst_ledger
            if 'debit_or_credit' in sgst_data:
                analyzed_bill.sgst_debit_or_credit = sgst_data['debit_or_credit']

            # Handle TDS data
            tds_data = taxes_data.get('tds', {})
            if 'amount' in tds_data:
                analyzed_bill.tds = _to_decimal(tds_data['amount'])
            if 'ledger' in tds_data and tds_data['ledger'] != "No Tax Ledger":
                tds_ledger = find_or_create_expense_tax_ledger(tds_data['ledger'], 'TDS', organization)
                if tds_ledger:
                    analyzed_bill.tds_taxes = tds_ledger
            if 'debit_or_credit' in tds_data:
                analyzed_bill.tds_debit_or_credit = tds_data['debit_or_credit']

            # Handle Other Adjustment data
            other_adjustment_data = taxes_data.get('other_adjustment', {})
            if 'amount' in other_adjustment_data:
                analyzed_bill.other_adjustment = _to_decimal(other_adjustment_data['amount'])
            if 'ledger' in other_adjustment_data and other_adjustment_data['ledger'] != "No Tax Ledger":
                other_adj_ledger = find_or_create_expense_tax_ledger(other_adjustment_data['ledger'], 'OTHER', organization)
                if other_adj_ledger:
                    analyzed_bill.other_adjustment_taxes = other_adj_ledger
            if 'debit_or_credit' in other_adjustment_data:
                analyzed_bill.other_adjustment_debit_or_credit = other_adjustment_data['debit_or_credit']

        # Determine GST type based on updated amounts
        if analyzed_bill.igst and analyzed_bill.igst > 0:
            analyzed_bill.gst_type = TallyExpenseAnalyzedBill.GSTType.IGST
        elif (analyzed_bill.cgst and analyzed_bill.cgst > 0) or (analyzed_bill.sgst and analyzed_bill.sgst > 0):
            analyzed_bill.gst_type = TallyExpenseAnalyzedBill.GSTType.CGST_SGST
        else:
            analyzed_bill.gst_type = TallyExpenseAnalyzedBill.GSTType.UNKNOWN

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
                    existing_consolidated = TallyExpenseConsolidatedProduct.objects.filter(expense_bill=analyzed_bill)
                    if existing_consolidated.exists():
                        existing_count = existing_consolidated.count()
                        existing_consolidated.delete()
                        logger.info(f"Deleted {existing_count} existing consolidated products before creating new ones")

                    # Create new consolidated products from payload
                    for idx, consolidated_data in enumerate(consolidate_prod_array):
                        logger.info(f"Creating consolidated expense product {idx + 1}: {consolidated_data.get('item_details', 'Unnamed')}")
                        
                        # Find chart of accounts ledger if specified
                        chart_ledger = None
                        chart_ledger_identifier = consolidated_data.get('chart_of_accounts')
                        if chart_ledger_identifier and chart_ledger_identifier != "No COA Ledger":
                            try:
                                # Check if it's a UUID (ledger ID)
                                import uuid
                                uuid.UUID(str(chart_ledger_identifier))
                                # It's a UUID, find ledger by ID
                                chart_ledger = Ledger.objects.filter(
                                    id=chart_ledger_identifier,
                                    organization=organization
                                ).first()
                                if chart_ledger:
                                    logger.info(f"Found chart of accounts ledger by UUID: {chart_ledger.name}")
                                else:
                                    logger.warning(f"Chart of accounts ledger not found for UUID: {chart_ledger_identifier}")
                            except (ValueError, TypeError):
                                # It's a name, find by name
                                chart_ledger = Ledger.objects.filter(
                                    name=chart_ledger_identifier,
                                    organization=organization
                                ).first()
                                if chart_ledger:
                                    logger.info(f"Found chart of accounts ledger by name: {chart_ledger.name}")
                                else:
                                    logger.warning(f"Chart of accounts ledger not found by name: {chart_ledger_identifier}")
                            except Exception as e:
                                logger.error(f"Error finding chart of accounts ledger: {e}")

                        # Create new consolidated product
                        consolidated_product = TallyExpenseConsolidatedProduct.objects.create(
                            expense_bill=analyzed_bill,
                            organization=organization,
                            item_details=consolidated_data.get('item_details', 'Consolidated expense from verification'),
                            amount=_to_decimal(consolidated_data.get('amount', 0)),
                            debit_or_credit=consolidated_data.get('debit_or_credit', 'debit'),
                            chart_of_accounts=chart_ledger,
                            original_entries_count=consolidated_data.get('original_entries_count', 1),
                            consolidation_notes='Created from frontend verification'
                        )
                        logger.info(f"Created consolidated expense product {consolidated_product.id} with chart of accounts: {chart_ledger.name if chart_ledger else 'None'}")
                        
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
            # INDIVIDUAL PRODUCTS MODE: Handle expense_items array
            logger.info("Processing in individual products mode")
            expense_items = analyzed_data.get('expense_items', [])
            
            if expense_items:
                logger.info(f"Processing {len(expense_items)} individual expense items")
                update_analyzed_expense_products(analyzed_bill, expense_items, organization)
                
                # Keep any existing consolidated products for layout switching support
                existing_consolidated_products = analyzed_bill.consolidated_products.all()
                if existing_consolidated_products.exists():
                    logger.info(f"✅ PRESERVED {existing_consolidated_products.count()} consolidated products (layout switching support)")
                    # Consolidated products are preserved for layout flexibility

        return analyzed_bill


def find_or_create_expense_chart_of_accounts_ledger(coa_name, organization):
    """Delegates to shared find_coa_ledger in helpers."""
    from .helpers import find_coa_ledger
    return find_coa_ledger(coa_name, organization)


def find_or_create_expense_vendor_ledger(vendor_name, vendor_data, organization):
    """Delegates to shared find_or_create_tally_vendor_ledger in helpers."""
    from .helpers import find_or_create_tally_vendor_ledger
    return find_or_create_tally_vendor_ledger(
        vendor_name, vendor_data, organization,
        default_parent_name=None, create_default_parent=False
    )


def find_or_create_expense_tax_ledger(ledger_name, tax_type, organization):
    """Delegates to shared find_or_create_tally_tax_ledger in helpers."""
    from .helpers import find_or_create_tally_tax_ledger
    return find_or_create_tally_tax_ledger(ledger_name, tax_type, organization)


def update_analyzed_expense_products(analyzed_bill, expense_items, organization):
    """Update existing expense products and create new ones based on item_id"""

    # Validate debit/credit balance before processing - including all components
    total_debit = 0
    total_credit = 0

    # Calculate debit/credit from expense items
    for item_data in expense_items:
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

    # Check if debit and credit amounts are equal (including all components)
    # (allowing for small rounding differences)
    if abs(total_debit - total_credit) > 0.01:
        raise Exception(
            f"Total Debit and Credit amounts must be equal across all components. "
            f"Total Debit: {total_debit}, Total Credit: {total_credit}, "
            f"Difference: {abs(total_debit - total_credit)}. "
            f"This includes expense items, taxes (IGST/CGST/SGST/TDS), and vendor amount."
        )

    # Get existing products mapped by their ID
    existing_products = {str(p.id): p for p in analyzed_bill.products.all()}
    updated_product_ids = set()

    for item_data in expense_items:
        item_id = item_data.get('item_id')  # Check for item_id in payload

        if item_id and str(item_id) in existing_products:
            # Update existing product
            product = existing_products[str(item_id)]
            updated_product_ids.add(str(item_id))
        else:
            # Create new product if item_id is missing or doesn't match existing
            product = TallyExpenseAnalyzedProduct(
                expense_bill=analyzed_bill,
                organization=organization
            )

        # Update product fields
        if 'item_details' in item_data:
            product.item_details = item_data['item_details']
        if 'amount' in item_data:
            product.amount = _to_decimal(item_data['amount'])
        if 'debit_or_credit' in item_data:
            product.debit_or_credit = item_data['debit_or_credit']

        # Handle chart of accounts ledger
        if 'chart_of_accounts' in item_data and item_data['chart_of_accounts'] != "No COA Ledger":
            coa_ledger = find_or_create_expense_tax_ledger(item_data['chart_of_accounts'], 'COA', organization)
            if coa_ledger:
                product.chart_of_accounts = coa_ledger

        product.save()

    # ✅ CRITICAL: Only delete expense products if we're NOT in consolidation mode
    # In consolidation mode, individual products are PRESERVED for layout switching
    if not getattr(analyzed_bill, 'consolidate', False):
        products_to_delete = []
        for existing_id, product in existing_products.items():
            if existing_id not in updated_product_ids:
                products_to_delete.append(product)
        
        if products_to_delete:
            deleted_count = len(products_to_delete)
            for product in products_to_delete:
                logger.info(f"Deleting expense product {product.id}: {product.item_details or 'Unknown'}")
                product.delete()
            logger.info(f"Deleted {deleted_count} expense products not present in frontend payload")
    else:
        logger.info("✅ CONSOLIDATION MODE: Individual products PRESERVED for layout switching")
        logger.info("✅ Users can switch between individual and consolidated layouts")
    
    # Calculate deletion count for summary  
    deletion_count = 0 if getattr(analyzed_bill, 'consolidate', False) else len([existing_id for existing_id in existing_products.keys() if existing_id not in updated_product_ids])
    
    logger.info(
        f"Expense product update summary: {len(updated_product_ids)} updated, "
        f"{len(expense_items or []) - len(updated_product_ids)} created, "
        f"{deletion_count} deleted (consolidate mode: {getattr(analyzed_bill, 'consolidate', False)})"
    )


def get_structured_expense_bill_data(analyzed_bill, organization):
    """Get structured expense bill data in the same format as detail view"""
    vendor_ledger = analyzed_bill.vendor
    analyzed_bill_products = analyzed_bill.products.all()
    bill_date_str = analyzed_bill.bill_date.strftime('%d-%m-%Y') if analyzed_bill.bill_date else None
    team_slug = organization.name if hasattr(organization, 'name') else str(organization.id)

    return {
        "vendor": {
            "master_id": vendor_ledger.master_id if vendor_ledger and vendor_ledger.master_id else "No Ledger",
            "name": vendor_ledger.name if vendor_ledger and vendor_ledger.name else "No Ledger",
            "gst_in": vendor_ledger.gst_in if vendor_ledger and vendor_ledger.gst_in else "No Ledger",
            "company": vendor_ledger.company if vendor_ledger and vendor_ledger.company else "No Ledger",
        },
        "bill_details": {
            "voucher": analyzed_bill.voucher or "",
            "bill_number": analyzed_bill.bill_no,
            "date": bill_date_str,
            "due_date": analyzed_bill.due_date.strftime('%d-%m-%Y') if analyzed_bill.due_date else None,
            "total_amount": float(analyzed_bill.total or 0),
            "company_id": team_slug,
        },
        "taxes": {
            "igst": {
                "amount": float(analyzed_bill.igst or 0),
                "ledger": str(analyzed_bill.igst_taxes) if analyzed_bill.igst_taxes else "No Tax Ledger",
                "debit_or_credit": analyzed_bill.igst_debit_or_credit or "debit",
            },
            "cgst": {
                "amount": float(analyzed_bill.cgst or 0),
                "ledger": str(analyzed_bill.cgst_taxes) if analyzed_bill.cgst_taxes else "No Tax Ledger",
                "debit_or_credit": analyzed_bill.cgst_debit_or_credit or "debit",
            },
            "sgst": {
                "amount": float(analyzed_bill.sgst or 0),
                "ledger": str(analyzed_bill.sgst_taxes) if analyzed_bill.sgst_taxes else "No Tax Ledger",
                "debit_or_credit": analyzed_bill.sgst_debit_or_credit or "debit",
            },
            "tds": {
                "amount": float(analyzed_bill.tds or 0),
                "ledger": str(analyzed_bill.tds_taxes) if analyzed_bill.tds_taxes else "No Tax Ledger",
                "debit_or_credit": analyzed_bill.tds_debit_or_credit or "debit",
            },
            "other_adjustment": {
                "amount": float(analyzed_bill.other_adjustment or 0),
                "ledger": str(analyzed_bill.other_adjustment_taxes) if analyzed_bill.other_adjustment_taxes else "No Tax Ledger",
                "debit_or_credit": analyzed_bill.other_adjustment_debit_or_credit or "debit",
            }
        },
        "expense_items": [
            {
                "id": str(item.id),  # Include ID for future updates
                "item_details": item.item_details,
                "chart_of_accounts": str(item.chart_of_accounts) if item.chart_of_accounts else "No COA Ledger",
                "amount": float(item.amount or 0),
                "debit_or_credit": item.debit_or_credit,
            }
            for item in analyzed_bill_products
        ],
    }


# ============================================================================
# Expense Bill Sync View
# ✅
@extend_schema(
    summary="Sync Expense Bill",
    description="Sync verified expense bill with Tally system",
    request=ExpenseBillSyncRequestSerializer,
    responses={200: ExpenseBillSyncResponseSerializer},
    tags=['Tally Expense Bills']
)
@api_view(['POST'])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def expense_bill_sync(request, org_id):
    """Sync verified expense bill with Tally"""
    serializer = ExpenseBillSyncRequestSerializer(data=request.data)
    if not serializer.is_valid():
        return Response({
            'error': 'Invalid Sync Request',
            'message': 'The expense bill sync request data is invalid. Please ensure the bill_id is provided and valid.',
            'details': serializer.errors,
            'error_code': 'INVALID_EXPENSE_SYNC_REQUEST'
        }, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

    bill_id = serializer.validated_data['bill_id']
    organization = get_organization_from_request(request, org_id)

    try:
        bill = TallyExpenseBill.objects.get(id=bill_id, organization=organization)
        analyzed_bill = TallyExpenseAnalyzedBill.objects.get(selected_bill=bill)
    except (TallyExpenseBill.DoesNotExist, TallyExpenseAnalyzedBill.DoesNotExist):
        return Response({
            'error': 'Expense Bill or Analysis Data Not Found',
            'message': f'Expense bill with ID {bill_id} or its analyzed data not found. Please ensure the expense bill exists and has been analyzed.',
            'error_code': 'EXPENSE_BILL_OR_ANALYSIS_NOT_FOUND'
        }, status=status.HTTP_404_NOT_FOUND)

    if bill.status != TallyExpenseBill.BillStatus.VERIFIED:
        return Response({
            'error': 'Expense Bill Not Ready for Sync',
            'message': f'Expense bill must be in "Verified" status to sync with Tally. Current status: {bill.status}. Please verify the expense bill first.',
            'current_status': bill.status,
            'required_status': 'Verified',
            'error_code': 'EXPENSE_BILL_NOT_VERIFIED'
        }, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

    try:
        # Get structured bill data in the same format as verify view
        sync_data = get_structured_expense_bill_data(analyzed_bill, organization)

        # Update bill status to synced
        bill.status = TallyExpenseBill.BillStatus.SYNCED
        bill.save(update_fields=['status'])

        # Send the payload to expense_bill_sync_external
        try:
            # Create a new request-like object with the sync data
            sync_response = expense_bill_sync_external_handler(sync_data, org_id, organization)

            return Response({
                "message": "Expense bill synced successfully",
                "bill_id": str(bill_id),
                "status": "Synced",
                "sync_data": sync_data,
                "external_sync": sync_response
            }, status=status.HTTP_200_OK)

        except Exception as sync_error:
            logger.warning(f"External expense sync failed but bill status updated: {str(sync_error)}")
            return Response({
                "message": "Expense bill synced successfully but external sync failed",
                "bill_id": str(bill_id),
                "status": "Synced",
                "sync_data": sync_data,
                "external_sync_error": str(sync_error)
            }, status=status.HTTP_200_OK)

    except Exception as e:
        logger.error(f"Expense bill sync failed: {str(e)}")
        return Response({
            'error': 'Expense Bill Sync Failed',
            'message': 'The expense bill sync process encountered an error. This could be due to Tally system connectivity issues or invalid expense data. Please verify your data and try again.',
            'details': str(e),
            'error_code': 'EXPENSE_SYNC_FAILED'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


def expense_bill_sync_external_handler(sync_data, org_id, organization):
    """Handle external sync with the provided payload"""
    try:
        # Log the sync attempt
        logger.info(f"External expense sync handler called for organization {organization.id}")
        logger.info(f"Expense sync data: {json.dumps(sync_data, indent=2)}")

        # Here you can add any external API calls or processing
        # For now, we'll just return a success response
        return {
            "status": "success",
            "message": "Expense payload received and processed",
            "data": sync_data
        }

    except Exception as e:
        logger.error(f"External expense sync handler failed: {str(e)}")
        raise Exception(f"External expense sync failed: {str(e)}")


# ============================================================================
# Delete Expense Bill
# ✅
@extend_schema(
    summary="Delete Expense Bill",
    description="Delete an expense bill and its associated file",
    responses={204: None},
    tags=['Tally Expense Bills']
)
@api_view(['DELETE'])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def expense_bill_delete(request, org_id, bill_id):
    """Delete expense bill."""
    from .bill_helpers import bill_delete_base
    return bill_delete_base(request, org_id, bill_id, TallyExpenseBill)


# ============================================================================
# Get Bills by Status
# ✅
@extend_schema(
    summary="Get Bills by Status",
    description="Get expense bills filtered by status",
    responses={200: TallyExpenseBillSerializer(many=True)},
    tags=['Tally Expense Bills']
)
@api_view(['GET'])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def expense_bills_by_status(request, org_id):
    """Get bills filtered by status."""
    from .bill_helpers import bills_by_status_base
    return bills_by_status_base(request, org_id, TallyExpenseBill, TallyExpenseBillSerializer)


# ============================================================================
# Tally TCP Integration Views
# ✅
@extend_schema(
    summary="Get All Synced Expense Bills",
    description="Get all synced expense bills with their products for the organization",
    responses={200: ExpenseBillSyncResponseSerializer(many=True)},
    tags=['Tally TCP']
)
@api_view(['GET'])
@permission_classes([OrganizationAPIKeyOrBearerToken])
def expense_bills_sync_list(request, org_id):
    """Get all synced expense bills with their products"""
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
    analyzed_bills = TallyExpenseAnalyzedBill.objects.filter(
        organization=organization,
        selected_bill__status=TallyExpenseBill.BillStatus.SYNCED,
        selected_bill__tally_synced=False
    ).select_related(
        'selected_bill', 'vendor', 'igst_taxes', 'cgst_taxes', 'sgst_taxes'
    ).prefetch_related(
        'products__chart_of_accounts'
    ).order_by('-created_at')

    # Convert each analyzed bill to the new sync format and extract just the data portion
    bills_data = []
    for analyzed_bill in analyzed_bills:
        sync_data = prepare_expense_sync_data(analyzed_bill, organization)
        # Extract the data portion (remove the wrapper)
        bills_data.append(sync_data["data"])

    # Return all bills under a single "data" key
    return Response({
        "data": bills_data
    }, status=status.HTTP_200_OK)


def prepare_expense_sync_data(analyzed_bill, organization):
    """Prepare expense bill data for Tally sync using structured format with consolidation support"""
    vendor_ledger = analyzed_bill.vendor
    bill_date_str = analyzed_bill.bill_date.strftime('%d-%m-%Y') if analyzed_bill.bill_date else None

    # Initialize DR and CR ledgers for expense sync
    dr_ledger = []
    cr_ledger = []

    # 🔄 SIMPLE CONSOLIDATION CHECK: Use consolidate flag to decide which data to use
    if hasattr(analyzed_bill, 'consolidate') and analyzed_bill.consolidate:
        # ✅ USE CONSOLIDATED TABLE DATA
        try:
            consolidated_expenses = analyzed_bill.consolidated_products.all()
            logger.info(f"Using consolidated data for expense bill {analyzed_bill.bill_no}")

            # Add consolidated expense entries
            for consolidated_expense in consolidated_expenses:
                if consolidated_expense.amount and consolidated_expense.amount > 0:
                    consolidated_entry = {
                        "LEDGERNAME": str(consolidated_expense.chart_of_accounts) if consolidated_expense.chart_of_accounts else "General Expenses",
                        "AMOUNT": float(consolidated_expense.amount)
                    }
                    # Use the debit_or_credit from consolidated product
                    if consolidated_expense.debit_or_credit == 'debit':
                        dr_ledger.append(consolidated_entry)
                    elif consolidated_expense.debit_or_credit == 'credit':
                        cr_ledger.append(consolidated_entry)

        except Exception as e:
            logger.error(f"Error accessing consolidated expense for bill {analyzed_bill.bill_no}: {e}")
            # Fallback to individual products if consolidated data fails
            analyzed_bill.consolidate = False  # Reset flag for this request

    if not hasattr(analyzed_bill, 'consolidate') or not analyzed_bill.consolidate:
        # ✅ USE INDIVIDUAL EXPENSE PRODUCTS (Original logic)
        analyzed_bill_products = analyzed_bill.products.all()
        logger.info(f"Using individual expense entries for bill {analyzed_bill.bill_no} ({analyzed_bill_products.count()} entries)")

        # Process expense line items based on their debit_or_credit field
        for item in analyzed_bill_products:
            if item.amount and item.amount > 0:
                ledger_entry = {
                    "LEDGERNAME": str(item.chart_of_accounts) if item.chart_of_accounts else "No COA Ledger",
                    "AMOUNT": float(item.amount)
                }

                # Simple rule: debit goes to DR_LEDGER, credit goes to CR_LEDGER
                if item.debit_or_credit == 'debit':
                    dr_ledger.append(ledger_entry)
                elif item.debit_or_credit == 'credit':
                    cr_ledger.append(ledger_entry)

    # Process IGST based on debit_or_credit field
    if analyzed_bill.igst and analyzed_bill.igst > 0 and analyzed_bill.igst_taxes:
        igst_entry = {
            "LEDGERNAME": str(analyzed_bill.igst_taxes),
            "AMOUNT": float(analyzed_bill.igst)
        }
        if analyzed_bill.igst_debit_or_credit == 'debit':
            dr_ledger.append(igst_entry)
        elif analyzed_bill.igst_debit_or_credit == 'credit':
            cr_ledger.append(igst_entry)

    # Process CGST based on debit_or_credit field
    if analyzed_bill.cgst and analyzed_bill.cgst > 0 and analyzed_bill.cgst_taxes:
        cgst_entry = {
            "LEDGERNAME": str(analyzed_bill.cgst_taxes),
            "AMOUNT": float(analyzed_bill.cgst)
        }
        if analyzed_bill.cgst_debit_or_credit == 'debit':
            dr_ledger.append(cgst_entry)
        elif analyzed_bill.cgst_debit_or_credit == 'credit':
            cr_ledger.append(cgst_entry)

    # Process SGST based on debit_or_credit field
    if analyzed_bill.sgst and analyzed_bill.sgst > 0 and analyzed_bill.sgst_taxes:
        sgst_entry = {
            "LEDGERNAME": str(analyzed_bill.sgst_taxes),
            "AMOUNT": float(analyzed_bill.sgst)
        }
        if analyzed_bill.sgst_debit_or_credit == 'debit':
            dr_ledger.append(sgst_entry)
        elif analyzed_bill.sgst_debit_or_credit == 'credit':
            cr_ledger.append(sgst_entry)

    # Process TDS based on debit_or_credit field
    if analyzed_bill.tds and analyzed_bill.tds > 0 and analyzed_bill.tds_taxes:
        tds_entry = {
            "LEDGERNAME": str(analyzed_bill.tds_taxes),
            "AMOUNT": float(analyzed_bill.tds)
        }
        if analyzed_bill.tds_debit_or_credit == 'debit':
            dr_ledger.append(tds_entry)
        elif analyzed_bill.tds_debit_or_credit == 'credit':
            cr_ledger.append(tds_entry)

    # Process Other Adjustment based on debit_or_credit field
    if analyzed_bill.other_adjustment and analyzed_bill.other_adjustment > 0 and analyzed_bill.other_adjustment_taxes:
        other_adjustment_entry = {
            "LEDGERNAME": str(analyzed_bill.other_adjustment_taxes),
            "AMOUNT": float(analyzed_bill.other_adjustment)
        }
        if analyzed_bill.other_adjustment_debit_or_credit == 'debit':
            dr_ledger.append(other_adjustment_entry)
        elif analyzed_bill.other_adjustment_debit_or_credit == 'credit':
            cr_ledger.append(other_adjustment_entry)

    # Process vendor based on vendor_debit_or_credit field using vendor_amount
    if vendor_ledger and analyzed_bill.vendor_amount and analyzed_bill.vendor_amount > 0:
        vendor_entry = {
            "LEDGERNAME": vendor_ledger.name,
            "AMOUNT": float(analyzed_bill.vendor_amount)
        }

        # Add vendor to appropriate ledger based on vendor_debit_or_credit
        if analyzed_bill.vendor_debit_or_credit == 'debit':
            dr_ledger.append(vendor_entry)
        elif analyzed_bill.vendor_debit_or_credit == 'credit':
            cr_ledger.append(vendor_entry)

    # Build expense sync payload with structured format similar to vendor bills
    vendor_name = vendor_ledger.name if vendor_ledger and vendor_ledger.name else "Unknown Vendor"

    # Construct the expense bill URL
    bill_url = f"https://billmunshi.com/tally/expense-bill/{analyzed_bill.selected_bill.id}"

    # Create the notes message
    notes_message = f"Bill from {vendor_name} entered via BillMunshi {bill_url}"

    bill_data = {
        "id": str(analyzed_bill.selected_bill.id),
        "voucher": analyzed_bill.voucher or "",
        "bill_no": analyzed_bill.bill_no or "",
        "bill_date": bill_date_str,
        "total": float(analyzed_bill.total or 0),
        "name": vendor_name,
        "company": vendor_ledger.company if vendor_ledger and vendor_ledger.company else "No Ledger",
        "gst_in": vendor_ledger.gst_in if vendor_ledger and vendor_ledger.gst_in else "No Ledger",
        "DR_LEDGER": dr_ledger,
        "CR_LEDGER": cr_ledger,
        "notes": notes_message,
        "created_at": analyzed_bill.created_at.isoformat() if analyzed_bill.created_at else None
    }

    return {"data": bill_data}


@extend_schema(
    summary="Sync Expense Bill to External System",
    description="Accept expense bill data payload for external system sync",
    responses={
        200: OpenApiResponse(description="Payload accepted successfully"),
        400: OpenApiResponse(description="Invalid payload")
    },
    tags=['Tally TCP']
)
@api_view(['POST'])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def expense_bill_sync_external(request, org_id):
    """Accept expense bill payload for external system sync"""
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
                'message': 'No payload data provided for external expense sync. Please provide the expense bill data to be processed.',
                'error_code': 'NO_EXPENSE_PAYLOAD_PROVIDED'
            },
            status=status.HTTP_422_UNPROCESSABLE_ENTITY
        )

    try:
        # Log the received payload
        logger.info(f"External expense sync received payload for organization {organization.id}")
        logger.info(f"Expense Payload: {json.dumps(payload, indent=2)}")

        # Here you can process the payload as needed
        # For now, we'll just acknowledge receipt

        return Response({
            'message': 'Expense payload received and processed successfully',
            'organization_id': str(organization.id),
            'payload_received': True,
            'timestamp': datetime.now().isoformat()
        }, status=status.HTTP_200_OK)

    except Exception as e:
        logger.error(f"External expense sync failed: {str(e)}")
        return Response({
            'error': 'External Expense Sync Processing Failed',
            'message': 'There was an error processing the external expense sync payload. This could be due to invalid data format or system issues.',
            'details': str(e),
            'error_code': 'EXTERNAL_EXPENSE_SYNC_FAILED'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

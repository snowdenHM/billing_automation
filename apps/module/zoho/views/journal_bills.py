import json
import logging
import os

import requests
from django.conf import settings
from django.db import transaction
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, parser_classes
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.utils import get_organization_from_request
from apps.common.services.duplicate_detection import check_duplicate_bill as _check_duplicate_bill_generic
from apps.common.services.pdf_processing import split_pdf_to_bills
from ..models import (
    JournalBill,
    JournalZohoBill,
    JournalZohoProduct,
    JournalZohoConsolidatedProduct,
    ZohoChartOfAccount,
    ZohoVendor,
)
from ..serializers.common import AnalysisResponseSerializer
from ..serializers.journal_bills import (
    ZohoJournalBillSerializer,
    ZohoJournalBillDetailSerializer,
    JournalZohoBillSerializer,
    ZohoJournalBillMultipleUploadSerializer,
)
from .helpers import (
    get_zoho_credentials,
    refresh_zoho_access_token,
    analyze_zoho_bill,
)
from .bill_view_helpers import (
    zoho_bills_list_base,
    zoho_bills_upload_base,
    zoho_bill_detail_base,
    zoho_bill_analyze_base,
    zoho_bill_delete_base,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Thin helper wrappers (parameterize shared services for journal bills)
# ============================================================================

def check_duplicate_journal_bill(bill, organization):
    """Wrapper: delegates to generic duplicate detection with JournalBill model."""
    return _check_duplicate_bill_generic(bill, organization, JournalBill)


def validate_zoho_journal_bill_ownership(json_data, organization):
    """Validate if the journal bill belongs to the organization — delegates to shared service.
    
    For journal bills (general ledger entries), we check the 'to' field against the organization.
    Journal entries typically record transactions where the org is the recipient.
    
    Returns a rich dict with ``is_valid``, ``confidence``, ``reason``,
    and ``validation_details``.
    """
    from apps.common.services.ownership import validate_bill_ownership as _core

    # For journal bills, check 'to' field with allow_empty=True (may lack clear customer info)
    result = _core(json_data, organization, check_field='to', 
                   allow_empty=True, bill_type='journal bill')
    return result


def process_pdf_splitting_journal(pdf_file, organization, file_type, uploaded_by):
    """Split PDF into per-page journal bills -- delegates to shared service."""
    return split_pdf_to_bills(
        pdf_file, organization, file_type, uploaded_by,
        bill_model=JournalBill, filename_prefix="BM-Journal-Page",
    )


def analyze_bill_with_openai(file_content, file_extension):
    """Analyze journal bill -- delegates to shared helper."""
    return analyze_zoho_bill(file_content, file_extension, label="journal")


def create_journal_zoho_objects_from_analysis(bill, analyzed_data, organization):
    """Create JournalZohoBill and JournalZohoProduct objects from analyzed data with ownership validation."""
    from .bill_creation import create_zoho_objects_from_analysis

    # Step 1: Validate bill ownership (external bill detection)
    try:
        ownership_result = validate_zoho_journal_bill_ownership(analyzed_data, organization)
        logger.info(f"Ownership validation result for journal bill {bill.id}: {ownership_result}")
        
        # Store ownership validation result in bill
        if hasattr(bill, 'bill_belong_your_org'):
            bill.bill_belong_your_org = ownership_result.get('is_valid', False)
        
        if hasattr(bill, 'description'):
            bill.description = ownership_result.get('reason') or bill.description or ''
        
        # Save fields if they exist
        update_fields = []
        if hasattr(bill, 'bill_belong_your_org'):
            update_fields.append('bill_belong_your_org')
        if hasattr(bill, 'description'):
            update_fields.append('description')
        
        if update_fields:
            bill.save(update_fields=update_fields)
            
    except Exception as e:
        logger.error(f"Ownership validation failed for journal bill {bill.id}: {str(e)}")
        # Continue with processing even if ownership validation fails
    
    # Step 2: Create Zoho objects using shared utilities
    return create_zoho_objects_from_analysis(
        bill, analyzed_data, organization,
        bill_model=JournalZohoBill,
        product_model=JournalZohoProduct,
        consolidated_model=JournalZohoConsolidatedProduct,
        ledger_type='journal',
        bill_type_label='journal',
        assign_taxes=False,
    )


# ============================================================================
# Views that delegate to bill_view_helpers
# ============================================================================

@extend_schema(
    responses=ZohoJournalBillSerializer(many=True),
    tags=["Zoho Journal Bills"],
    methods=["GET"],
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def journal_bills_list_view(request, org_id):
    """List all journal bills for the organization with pagination."""
    return zoho_bills_list_base(
        request, org_id,
        bill_model=JournalBill,
        list_serializer=ZohoJournalBillSerializer,
    )


@extend_schema(
    summary="Upload Journal Bills",
    description="Upload single or multiple journal bill files (PDF, JPG, PNG).",
    request=ZohoJournalBillMultipleUploadSerializer,
    responses={201: ZohoJournalBillSerializer(many=True)},
    tags=["Zoho Journal Bills"],
    methods=["POST"],
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser])
def journal_bill_upload_view(request, org_id):
    """Handle single or multiple journal bill file uploads with PDF splitting support."""
    from ..tasks import enqueue_journal_bill_analysis, split_pdf_bill_journal

    return zoho_bills_upload_base(
        request, org_id,
        bill_model=JournalBill,
        list_serializer=ZohoJournalBillSerializer,
        upload_serializer=ZohoJournalBillMultipleUploadSerializer,
        label='Journal',
        check_duplicate_fn=check_duplicate_journal_bill,
        split_pdf_fn=process_pdf_splitting_journal,
        analyze_fn=analyze_bill_with_openai,
        create_objects_fn=create_journal_zoho_objects_from_analysis,
        enqueue_analysis_fn=enqueue_journal_bill_analysis,  # Enable background processing
        pdf_split_task_fn=split_pdf_bill_journal,
    )


@extend_schema(
    responses=ZohoJournalBillDetailSerializer,
    tags=["Zoho Journal Bills"],
    methods=["GET"],
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def journal_bill_detail_view(request, org_id, bill_id):
    """Get journal bill details including analysis data."""
    return zoho_bill_detail_base(
        request, org_id, bill_id,
        bill_model=JournalBill,
        zoho_bill_model=JournalZohoBill,
        detail_serializer=ZohoJournalBillDetailSerializer,
        label='Journal',
        select_related=['vendor'],
        prefetch_related=['products__chart_of_accounts', 'consolidated_products__chart_of_accounts'],
    )


@extend_schema(
    responses=AnalysisResponseSerializer,
    tags=["Zoho Journal Bills"],
    methods=["POST"],
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def journal_bill_analyze_view(request, org_id, bill_id):
    """Analyze journal bill using AI. Changes status from 'Draft' to 'Analysed'."""
    return zoho_bill_analyze_base(
        request, org_id, bill_id,
        bill_model=JournalBill,
        label='Journal',
        check_duplicate_fn=check_duplicate_journal_bill,
        analyze_fn=analyze_bill_with_openai,
        create_objects_fn=create_journal_zoho_objects_from_analysis,
    )


@extend_schema(
    responses={"200": {"detail": "Journal bill deleted successfully"}},
    tags=["Zoho Journal Bills"],
    methods=["DELETE"],
)
@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def journal_bill_delete_view(request, org_id, bill_id):
    """Delete a journal bill and its associated file."""
    return zoho_bill_delete_base(
        request, org_id, bill_id,
        bill_model=JournalBill,
        label='Journal',
    )


# ============================================================================
# Verify view -- journal-specific (products use chart_of_accounts object,
# debit_or_credit field, debit/credit balance validation)
# ============================================================================

@extend_schema(
    request=JournalZohoBillSerializer,
    responses=JournalZohoBillSerializer,
    tags=["Zoho Journal Bills"],
    methods=["POST"],
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def journal_bill_verify_view(request, org_id, bill_id):
    """Verify and update journal bill data. Changes status from 'Analysed' to 'Verified'."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        payload_bill_id = request.data.get('bill_id', bill_id)
        zoho_bill_data = request.data.get('zoho_bill', request.data)

        # Validate vendor exists
        vendor_data = zoho_bill_data.get('vendor')
        if vendor_data:
            try:
                ZohoVendor.objects.get(id=vendor_data, organization=organization)
            except ZohoVendor.DoesNotExist:
                return Response(
                    {"detail": f"Vendor with ID {vendor_data} does not exist in this organization. Please sync vendors from Zoho first."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        bill = JournalBill.objects.get(id=payload_bill_id, organization=organization)

        if bill.status not in ['Analysed', 'Verified']:
            return Response(
                {"detail": "Bill must be in 'Analysed' or 'Verified' status to save"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Get existing JournalZohoBill
        try:
            zoho_bill = JournalZohoBill.objects.get(selectBill=bill, organization=organization)
        except JournalZohoBill.DoesNotExist:
            return Response(
                {"detail": "No analyzed journal data found. Please analyze the bill first."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            serializer = JournalZohoBillSerializer(
                zoho_bill, data=zoho_bill_data, partial=True,
                context={'organization': organization},
            )

            if not serializer.is_valid():
                return _handle_journal_verify_errors(serializer.errors, zoho_bill_data)

            updated_bill = serializer.save()

            # Handle products
            products_data = zoho_bill_data.get('products')
            if products_data is not None:
                _update_journal_products(updated_bill, products_data, organization)

            # Handle consolidated products
            consolidate_prod_data = zoho_bill_data.get('consolidate_prod', [])
            if consolidate_prod_data:
                _update_journal_consolidated(updated_bill, consolidate_prod_data, organization)

            # Handle consolidation flag
            consolidate_flag = zoho_bill_data.get('consolidate', False)
            if consolidate_flag != updated_bill.consolidate:
                updated_bill.consolidate = consolidate_flag
                updated_bill.save()

            # Validate debit/credit balance before setting Verified
            balance_error = _validate_debit_credit_balance(updated_bill)
            if balance_error:
                return balance_error

            # Update bill status to Verified
            bill.status = 'Verified'
            bill.save()

            return Response(
                JournalZohoBillSerializer(updated_bill, context={'organization': organization}).data
            )

    except JournalBill.DoesNotExist:
        return Response({"detail": "Journal bill not found"}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        logger.error(f"Journal verify failed: {e}", exc_info=True)
        return Response({"detail": f"Verification failed: {e}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ============================================================================
# Sync view -- journal-specific (Zoho /journals API with mandatory vendor
# line item, IGST/CGST/SGST line items, debit_or_credit on each line)
# ============================================================================

@extend_schema(
    responses={"200": {"detail": "Journal synced to Zoho successfully"}},
    tags=["Zoho Journal Bills"],
    methods=["POST"],
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def journal_bill_sync_view(request, org_id, bill_id):
    """Sync verified journal bill to Zoho Books. Changes status to 'Synced'."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        bill = JournalBill.objects.get(id=bill_id, organization=organization)

        if bill.status != 'Verified':
            return Response(
                {"detail": "Bill must be in 'Verified' status to sync"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            zoho_bill = JournalZohoBill.objects.get(selectBill=bill, organization=organization)
        except JournalZohoBill.DoesNotExist:
            return Response(
                {"detail": "Zoho journal data not found. Please verify the bill first."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            current_token = get_zoho_credentials(organization)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        zoho_products = zoho_bill.products.all()
        if not zoho_products.exists():
            return Response({"detail": "No products found for this bill"}, status=status.HTTP_400_BAD_REQUEST)

        # Build journal payload
        bill_date_str = zoho_bill.bill_date.strftime('%Y-%m-%d') if zoho_bill.bill_date else None

        bill_data = {
            "reference_number": zoho_bill.bill_no,
            "journal_date": bill_date_str,
            "notes": zoho_bill.note,
            "line_items": [],
        }

        # Mandatory vendor line item
        if not (zoho_bill.vendor_coa and zoho_bill.vendor_amount):
            return Response(
                {"detail": "Vendor chart of account and amount are mandatory for journal entry"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        bill_data["line_items"].append({
            "customer_id": str(zoho_bill.vendor.contactId),
            "description": f"Vendor - {zoho_bill.vendor.companyName if zoho_bill.vendor else 'Unknown Vendor'}",
            "account_id": str(zoho_bill.vendor_coa.accountId),
            "amount": float(zoho_bill.vendor_amount),
            "debit_or_credit": zoho_bill.vendor_debit_or_credit or "credit",
        })

        # Tax line items (IGST / CGST / SGST)
        _add_tax_line_item(bill_data["line_items"], zoho_bill.igst, zoho_bill.igst_coa, zoho_bill.igst_debit_or_credit, "IGST")
        _add_tax_line_item(bill_data["line_items"], zoho_bill.cgst, zoho_bill.cgst_coa, zoho_bill.cgst_debit_or_credit, "CGST")
        _add_tax_line_item(bill_data["line_items"], zoho_bill.sgst, zoho_bill.sgst_coa, zoho_bill.sgst_debit_or_credit, "SGST")

        # Product line items (consolidated or individual)
        _append_journal_product_lines(bill_data["line_items"], zoho_bill, zoho_products)

        if not bill_data["line_items"]:
            return Response({"detail": "No valid line items found for syncing"}, status=status.HTTP_400_BAD_REQUEST)

        logger.info(f"[JOURNAL SYNC] Syncing bill {bill_id} with {len(bill_data['line_items'])} line items")

        # Call Zoho API
        url = f"https://www.zohoapis.in/books/v3/journals?organization_id={current_token.organisationId}"
        headers = {
            'Authorization': f'Zoho-oauthtoken {current_token.accessToken}',
            'Content-Type': 'application/json',
        }

        try:
            payload = json.dumps(bill_data)
            response = requests.post(url, headers=headers, data=payload)

            if response.status_code == 401:
                new_access_token = refresh_zoho_access_token(current_token)
                if new_access_token:
                    headers['Authorization'] = f'Zoho-oauthtoken {new_access_token}'
                    response = requests.post(url, headers=headers, data=payload)

            if response.status_code == 201:
                bill.status = 'Synced'
                bill.save()
                response_data = response.json()
                return Response({
                    "detail": "Journal synced to Zoho successfully",
                    "zoho_journal_id": response_data.get('journal', {}).get('journal_id'),
                })
            else:
                response_json = response.json() if response.content else {}
                error_message = response_json.get("message", "Failed to send data to Zoho")
                logger.error(f"[JOURNAL SYNC] Zoho API error {response.status_code}: {error_message}")
                return Response({"detail": error_message}, status=status.HTTP_400_BAD_REQUEST)

        except requests.RequestException as e:
            logger.error(f"[JOURNAL SYNC] Network error: {e}")
            return Response({"detail": f"Network error: {e}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    except JournalBill.DoesNotExist:
        return Response({"detail": "Journal bill not found"}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        logger.error(f"[JOURNAL SYNC] Failed: {e}", exc_info=True)
        return Response({"detail": f"Sync failed: {e}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ============================================================================
# Private helpers for verify / sync
# ============================================================================

def _update_journal_products(updated_bill, products_data, organization):
    """Update/create/delete journal products during verify."""
    existing_products = {str(p.id): p for p in updated_bill.products.all()}
    processed_ids = set()

    for product_data in products_data:
        if not product_data.get('item_details'):
            continue

        product_id = product_data.get('id')

        # Validate chart_of_accounts
        chart_id = product_data.get('chart_of_accounts')
        chart_obj = None
        if chart_id:
            try:
                chart_obj = ZohoChartOfAccount.objects.get(id=chart_id, organization=organization)
            except ZohoChartOfAccount.DoesNotExist:
                raise ValueError(f"Chart of Account with ID {chart_id} does not exist in this organization.")

        product_fields = {
            'item_details': product_data.get('item_details'),
            'chart_of_accounts': chart_obj,
            'amount': product_data.get('amount'),
            'debit_or_credit': product_data.get('debit_or_credit', 'credit'),
        }
        product_fields = {k: v for k, v in product_fields.items() if v is not None}

        if product_id and str(product_id) in existing_products:
            existing = existing_products[str(product_id)]
            for field, value in product_fields.items():
                setattr(existing, field, value)
            existing.save()
            processed_ids.add(str(product_id))
        else:
            new_product = JournalZohoProduct.objects.create(
                zohoBill=updated_bill, organization=organization, **product_fields,
            )
            processed_ids.add(str(new_product.id))

    # Delete removed products
    to_delete = set(existing_products.keys()) - processed_ids
    if to_delete:
        JournalZohoProduct.objects.filter(id__in=to_delete, zohoBill=updated_bill).delete()


def _update_journal_consolidated(updated_bill, consolidate_prod_data, organization):
    """Replace consolidated products during verify."""
    try:
        updated_bill.consolidated_products.all().delete()

        for consolidated_data in consolidate_prod_data:
            JournalZohoConsolidatedProduct.objects.create(
                zohoBill=updated_bill,
                organization=organization,
                consolidated_item_details=consolidated_data.get('item_details', 'Consolidated journal entry from verification'),
                consolidated_amount=consolidated_data.get('amount', 0),
                debit_or_credit=consolidated_data.get('debit_or_credit', 'debit'),
                chart_of_accounts_id=consolidated_data.get('chart_of_accounts'),
                original_entries_count=1,
                consolidation_notes='Created from frontend verification',
            )
    except Exception as e:
        logger.error(f"Error processing journal consolidate_prod: {e}")


def _validate_debit_credit_balance(updated_bill):
    """Validate that total debits equal total credits. Returns error Response or None."""
    total_debit = 0.0
    total_credit = 0.0

    def _add_amount(value, dc_type):
        nonlocal total_debit, total_credit
        if not value:
            return
        try:
            amount = float(value)
            if amount <= 0:
                return
            if dc_type == 'debit':
                total_debit += amount
            elif dc_type == 'credit':
                total_credit += amount
        except (ValueError, TypeError):
            pass

    # Vendor amount
    _add_amount(updated_bill.vendor_amount, updated_bill.vendor_debit_or_credit)
    # Tax amounts
    _add_amount(updated_bill.igst, updated_bill.igst_debit_or_credit)
    _add_amount(updated_bill.cgst, updated_bill.cgst_debit_or_credit)
    _add_amount(updated_bill.sgst, updated_bill.sgst_debit_or_credit)
    # Product amounts
    for product in updated_bill.products.all():
        _add_amount(product.amount, product.debit_or_credit)

    tolerance = 0.01
    if abs(total_debit - total_credit) > tolerance:
        return Response({
            "detail": f"Debit and credit amounts must be equal. Current totals: Debit: {total_debit:.2f}, Credit: {total_credit:.2f}",
            "debit_total": total_debit,
            "credit_total": total_credit,
            "difference": abs(total_debit - total_credit),
        }, status=status.HTTP_400_BAD_REQUEST)

    return None


def _add_tax_line_item(line_items, tax_amount, tax_coa, dc_type, label):
    """Append a tax (IGST/CGST/SGST) line item if amount > 0."""
    if tax_amount and float(tax_amount or 0) > 0 and tax_coa:
        line_items.append({
            "description": label,
            "account_id": str(tax_coa.accountId),
            "amount": float(tax_amount),
            "debit_or_credit": dc_type or "debit",
        })


def _append_journal_product_lines(line_items, zoho_bill, zoho_products):
    """Append product line items (consolidated or individual) for journal sync."""
    use_consolidated = getattr(zoho_bill, 'consolidate', False)

    if use_consolidated:
        try:
            for cp in zoho_bill.consolidated_products.all():
                if cp.chart_of_accounts:
                    line_items.append({
                        "description": cp.consolidated_item_details or "Consolidated Journal Items",
                        "account_id": str(cp.chart_of_accounts.accountId),
                        "amount": float(cp.consolidated_amount) if cp.consolidated_amount else 0,
                        "debit_or_credit": getattr(cp, 'debit_or_credit', 'debit'),
                    })
            return  # Successfully used consolidated
        except Exception as e:
            logger.error(f"Error using consolidated products, falling back: {e}")

    # Individual products
    for item in zoho_products:
        try:
            if not item.chart_of_accounts:
                continue
            line_items.append({
                "description": item.item_details or f"Product Item {item.id}",
                "account_id": str(item.chart_of_accounts.accountId),
                "amount": float(item.amount) if item.amount else 0,
                "debit_or_credit": getattr(item, 'debit_or_credit', 'debit'),
            })
        except Exception as e:
            logger.error(f"Error processing journal product {item.id}: {e}")
            continue


def _handle_journal_verify_errors(errors, zoho_bill_data):
    """Map serializer errors to user-friendly messages."""
    if 'vendor' in errors:
        vendor_error = errors['vendor'][0] if errors['vendor'] else 'Unknown vendor error'
        if 'does not exist' in str(vendor_error):
            return Response({
                "detail": f"Vendor with ID {zoho_bill_data.get('vendor', 'Unknown')} does not exist. Please sync vendors from Zoho Books first.",
                "error_type": "vendor_not_found",
            }, status=status.HTTP_400_BAD_REQUEST)

    return Response(errors, status=status.HTTP_400_BAD_REQUEST)

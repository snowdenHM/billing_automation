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
    ExpenseBill,
    ExpenseZohoBill,
    ExpenseZohoProduct,
    ExpenseZohoConsolidatedProduct,
    ZohoChartOfAccount,
    ZohoTaxes,
    ZohoVendor,
)
from ..serializers.common import AnalysisResponseSerializer
from ..serializers.expense_bills import (
    ZohoExpenseBillSerializer,
    ZohoExpenseBillDetailSerializer,
    ExpenseZohoBillSerializer,
    ZohoExpenseBillMultipleUploadSerializer,
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
# Thin helper wrappers (parameterize shared services for expense bills)
# ============================================================================

def check_duplicate_expense_bill(bill, organization):
    """Wrapper: delegates to generic duplicate detection with ExpenseBill model."""
    return _check_duplicate_bill_generic(bill, organization, ExpenseBill)


def validate_zoho_expense_bill_ownership(json_data, organization):
    """Validate if the expense bill belongs to the organization — delegates to shared service.
    
    For expense bills, we check the 'to' field (customer/recipient) against the organization.
    Unlike vendor bills where suppliers bill the org, expense bills are typically receipts
    where the org is the customer.
    
    Returns a rich dict with ``is_valid``, ``confidence``, ``reason``,
    and ``validation_details``.
    """
    from apps.common.services.ownership import validate_bill_ownership as _core

    # For expense bills, check 'to' field with allow_empty=True (common for receipts)
    result = _core(json_data, organization, check_field='to', 
                   allow_empty=True, bill_type='expense bill')
    return result


def process_pdf_splitting_expense(pdf_file, organization, file_type, uploaded_by):
    """Split PDF into per-page expense bills -- delegates to shared service."""
    return split_pdf_to_bills(
        pdf_file, organization, file_type, uploaded_by,
        bill_model=ExpenseBill, filename_prefix="BM-Expense-Page",
    )


def analyze_bill_with_openai(file_content, file_extension):
    """Analyze expense bill -- delegates to shared helper."""
    return analyze_zoho_bill(file_content, file_extension, label="expense")


def create_expense_zoho_objects_from_analysis(bill, analyzed_data, organization):
    """Create ExpenseZohoBill and ExpenseZohoProduct objects from analyzed data with ownership validation."""
    from .bill_creation import create_zoho_objects_from_analysis

    # Step 1: Validate bill ownership (external bill detection)
    try:
        ownership_result = validate_zoho_expense_bill_ownership(analyzed_data, organization)
        logger.info(f"Ownership validation result for expense bill {bill.id}: {ownership_result}")
        
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
        logger.error(f"Ownership validation failed for expense bill {bill.id}: {str(e)}")
        # Continue with processing even if ownership validation fails
    
    # Step 2: Create Zoho objects using shared utilities
    return create_zoho_objects_from_analysis(
        bill, analyzed_data, organization,
        bill_model=ExpenseZohoBill,
        product_model=ExpenseZohoProduct,
        consolidated_model=ExpenseZohoConsolidatedProduct,
        ledger_type='expense',
        bill_type_label='expense',
        assign_taxes=True,
    )


# ============================================================================
# Views that delegate to bill_view_helpers
# ============================================================================

@extend_schema(
    responses=ZohoExpenseBillSerializer(many=True),
    tags=["Zoho Expense Bills"],
    methods=["GET"],
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def expense_bills_list_view(request, org_id):
    """List all expense bills for the organization with pagination."""
    return zoho_bills_list_base(
        request, org_id,
        bill_model=ExpenseBill,
        list_serializer=ZohoExpenseBillSerializer,
    )


@extend_schema(
    summary="Upload Expense Bills",
    description="Upload single or multiple expense bill files (PDF, JPG, PNG).",
    request=ZohoExpenseBillMultipleUploadSerializer,
    responses={201: ZohoExpenseBillSerializer(many=True)},
    tags=["Zoho Expense Bills"],
    methods=["POST"],
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser])
def expense_bill_upload_view(request, org_id):
    """Handle single or multiple expense bill file uploads with PDF splitting support."""
    from ..tasks import enqueue_expense_bill_analysis
    
    return zoho_bills_upload_base(
        request, org_id,
        bill_model=ExpenseBill,
        list_serializer=ZohoExpenseBillSerializer,
        upload_serializer=ZohoExpenseBillMultipleUploadSerializer,
        label='Expense',
        check_duplicate_fn=check_duplicate_expense_bill,
        split_pdf_fn=process_pdf_splitting_expense,
        analyze_fn=analyze_bill_with_openai,
        create_objects_fn=create_expense_zoho_objects_from_analysis,
        enqueue_analysis_fn=enqueue_expense_bill_analysis,  # Enable background processing
    )


@extend_schema(
    responses=ZohoExpenseBillDetailSerializer,
    tags=["Zoho Expense Bills"],
    methods=["GET"],
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def expense_bill_detail_view(request, org_id, bill_id):
    """Get expense bill details including analysis data."""
    return zoho_bill_detail_base(
        request, org_id, bill_id,
        bill_model=ExpenseBill,
        zoho_bill_model=ExpenseZohoBill,
        detail_serializer=ZohoExpenseBillDetailSerializer,
        label='Expense',
        select_related=['selectBill'],
        prefetch_related=['products__chart_of_accounts', 'products__taxes', 'consolidated_products'],
    )


@extend_schema(
    responses=AnalysisResponseSerializer,
    tags=["Zoho Expense Bills"],
    methods=["POST"],
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def expense_bill_analyze_view(request, org_id, bill_id):
    """Analyze expense bill using AI. Changes status from 'Draft' to 'Analysed'."""
    return zoho_bill_analyze_base(
        request, org_id, bill_id,
        bill_model=ExpenseBill,
        label='Expense',
        check_duplicate_fn=check_duplicate_expense_bill,
        analyze_fn=analyze_bill_with_openai,
        create_objects_fn=create_expense_zoho_objects_from_analysis,
    )


@extend_schema(
    responses={"200": {"detail": "Expense bill deleted successfully"}},
    tags=["Zoho Expense Bills"],
    methods=["DELETE"],
)
@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def expense_bill_delete_view(request, org_id, bill_id):
    """Delete an expense bill and its associated file."""
    return zoho_bill_delete_base(
        request, org_id, bill_id,
        bill_model=ExpenseBill,
        label='Expense',
    )


# ============================================================================
# Verify view -- expense-specific (products have taxes_id, no debit/credit)
# ============================================================================

@extend_schema(
    request=ExpenseZohoBillSerializer,
    responses=ExpenseZohoBillSerializer,
    tags=["Zoho Expense Bills"],
    methods=["POST"],
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def expense_bill_verify_view(request, org_id, bill_id):
    """Verify and update expense bill data after analysis."""
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

        bill = ExpenseBill.objects.get(id=payload_bill_id, organization=organization)

        if bill.status not in ['Analysed', 'Verified']:
            return Response(
                {"detail": "Bill must be in 'Analysed' or 'Verified' status to save"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Get existing ExpenseZohoBill
        try:
            zoho_bill = ExpenseZohoBill.objects.get(selectBill=bill, organization=organization)
        except ExpenseZohoBill.DoesNotExist:
            return Response(
                {"detail": "No analyzed expense data found. Please analyze the bill first."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            serializer = ExpenseZohoBillSerializer(
                zoho_bill, data=zoho_bill_data, partial=True,
                context={'organization': organization},
            )

            if not serializer.is_valid():
                return _handle_verify_errors(serializer.errors, zoho_bill_data)

            updated_bill = serializer.save()

            # Handle products
            products_data = zoho_bill_data.get('products')
            if products_data is not None:
                _update_expense_products(updated_bill, products_data, organization)

            # Handle consolidated products
            consolidate_prod_data = zoho_bill_data.get('consolidate_prod', [])
            if consolidate_prod_data:
                _update_expense_consolidated(updated_bill, consolidate_prod_data, organization)

            # Handle consolidation flag
            consolidate_flag = zoho_bill_data.get('consolidate', False)
            if consolidate_flag != updated_bill.consolidate:
                updated_bill.consolidate = consolidate_flag
                updated_bill.save()

            # Update bill status to Verified
            bill.status = 'Verified'
            bill.save()

            return Response(
                ExpenseZohoBillSerializer(updated_bill, context={'organization': organization}).data
            )

    except ExpenseBill.DoesNotExist:
        return Response({"detail": "Expense bill not found"}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        logger.error(f"Expense verify failed: {e}", exc_info=True)
        return Response({"detail": f"Verification failed: {e}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ============================================================================
# Sync view -- expense-specific (Zoho /expenses API)
# ============================================================================

@extend_schema(
    responses={"200": {"detail": "Expense bill synced to Zoho successfully"}},
    tags=["Zoho Expense Bills"],
    methods=["POST"],
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def expense_bill_sync_view(request, org_id, bill_id):
    """Sync verified expense bill to Zoho Books as an expense entry."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        bill = ExpenseBill.objects.get(id=bill_id, organization=organization)

        if bill.status != 'Verified':
            return Response(
                {"detail": "Bill must be in 'Verified' status to sync"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            current_token = get_zoho_credentials(organization)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        try:
            zoho_bill = ExpenseZohoBill.objects.get(selectBill=bill, organization=organization)
            zoho_products = ExpenseZohoProduct.objects.filter(zohoBill=zoho_bill)
        except ExpenseZohoBill.DoesNotExist:
            return Response({"detail": "No analyzed expense data found"}, status=status.HTTP_400_BAD_REQUEST)

        if not zoho_products.exists():
            return Response({"detail": "No expense items found to sync"}, status=status.HTTP_400_BAD_REQUEST)

        # Build line items
        line_items, total_amount = _build_expense_line_items(zoho_bill, zoho_products)

        if not zoho_bill.chart_of_accounts:
            return Response(
                {"detail": "No chart of account found for the expense. Please verify the bill first."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not line_items:
            return Response({"detail": "No valid expense items found for syncing"}, status=status.HTTP_400_BAD_REQUEST)

        expense_date_str = zoho_bill.bill_date.strftime('%Y-%m-%d') if zoho_bill.bill_date else None
        account_name = str(zoho_bill.chart_of_accounts.accountName)

        expense_data = {
            "paid_through_account_name": account_name,
            "date": expense_date_str,
            "amount": str(total_amount),
            "reference_number": str(zoho_bill.bill_no) if zoho_bill.bill_no else "",
            "description": zoho_bill.note or f"Expense from {zoho_bill.vendor.companyName if zoho_bill.vendor else 'Unknown Vendor'}",
            "vendor_id": str(zoho_bill.vendor.contactId) if zoho_bill.vendor else "",
            "gst_treatment": zoho_bill.vendor.gst_treatment if zoho_bill.vendor else "",
            "gst_no": str(zoho_bill.vendor.gstNo) if zoho_bill.vendor and zoho_bill.vendor.gstNo else "",
            "line_items": line_items,
        }

        logger.info(f"[EXPENSE SYNC] Syncing bill {bill_id} with {len(line_items)} line items")

        # Call Zoho API
        url = f"https://www.zohoapis.in/books/v3/expenses?organization_id={current_token.organisationId}"
        headers = {
            'Authorization': f'Zoho-oauthtoken {current_token.accessToken}',
            'Content-Type': 'application/json',
        }

        try:
            response = requests.post(url, headers=headers, data=json.dumps(expense_data))

            if response.status_code == 401:
                new_access_token = refresh_zoho_access_token(current_token)
                if new_access_token:
                    headers['Authorization'] = f'Zoho-oauthtoken {new_access_token}'
                    response = requests.post(url, headers=headers, data=json.dumps(expense_data))

            if response.status_code == 201:
                bill.status = 'Synced'
                bill.save()
                response_data = response.json()
                return Response({
                    "detail": "Expense bill synced to Zoho successfully",
                    "zoho_expense_id": response_data.get('expense', {}).get('expense_id'),
                })
            else:
                response_json = response.json() if response.content else {}
                error_message = response_json.get("message", "Failed to send expense to Zoho")
                logger.error(f"[EXPENSE SYNC] Zoho API error {response.status_code}: {error_message}")
                return Response({"detail": error_message}, status=status.HTTP_400_BAD_REQUEST)

        except requests.RequestException as e:
            logger.error(f"[EXPENSE SYNC] Network error: {e}")
            return Response({"detail": f"Network error: {e}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    except ExpenseBill.DoesNotExist:
        return Response({"detail": "Expense bill not found"}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        logger.error(f"[EXPENSE SYNC] Failed: {e}", exc_info=True)
        return Response({"detail": f"Expense sync failed: {e}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ============================================================================
# Private helpers for verify / sync
# ============================================================================

def _update_expense_products(updated_bill, products_data, organization):
    """Update/create/delete expense products during verify."""
    existing_products = {str(p.id): p for p in updated_bill.products.all()}
    processed_ids = set()

    for product_data in products_data:
        if not product_data.get('item_details'):
            continue

        product_id = product_data.get('id')

        # Validate chart_of_accounts
        chart_id = product_data.get('chart_of_accounts')
        if chart_id:
            try:
                ZohoChartOfAccount.objects.get(id=chart_id, organization=organization)
            except ZohoChartOfAccount.DoesNotExist:
                raise ValueError(f"Chart of Account with ID {chart_id} does not exist in this organization.")

        # Validate taxes
        tax_id = product_data.get('taxes')
        if tax_id:
            try:
                ZohoTaxes.objects.get(id=tax_id, organization=organization)
            except ZohoTaxes.DoesNotExist:
                raise ValueError(f"Tax with ID {tax_id} does not exist in this organization.")

        product_fields = {
            'item_details': product_data.get('item_details'),
            'chart_of_accounts_id': chart_id,
            'taxes_id': tax_id,
            'amount': product_data.get('amount'),
        }
        product_fields = {k: v for k, v in product_fields.items() if v is not None}

        if product_id and str(product_id) in existing_products:
            existing = existing_products[str(product_id)]
            for field, value in product_fields.items():
                setattr(existing, field, value)
            existing.save()
            processed_ids.add(str(product_id))
        else:
            new_product = ExpenseZohoProduct.objects.create(
                zohoBill=updated_bill, organization=organization, **product_fields,
            )
            processed_ids.add(str(new_product.id))

    # Delete removed products
    to_delete = set(existing_products.keys()) - processed_ids
    if to_delete:
        ExpenseZohoProduct.objects.filter(id__in=to_delete, zohoBill=updated_bill).delete()


def _update_expense_consolidated(updated_bill, consolidate_prod_data, organization):
    """Replace consolidated products during verify."""
    try:
        updated_bill.consolidated_products.all().delete()

        for consolidated_data in consolidate_prod_data:
            ExpenseZohoConsolidatedProduct.objects.create(
                zohoBill=updated_bill,
                organization=organization,
                consolidated_item_details=consolidated_data.get('item_details', 'Consolidated expense from verification'),
                consolidated_amount=consolidated_data.get('amount', 0),
                chart_of_accounts_id=consolidated_data.get('chart_of_accounts'),
                taxes_id=consolidated_data.get('taxes'),
                original_entries_count=1,
                consolidation_notes='Created from frontend verification',
            )
    except Exception as e:
        logger.error(f"Error processing consolidate_prod: {e}")


def _build_expense_line_items(zoho_bill, zoho_products):
    """Build Zoho expense line items -- handles consolidated vs individual products."""
    line_items = []
    total_amount = 0
    item_order = 1

    use_consolidated = getattr(zoho_bill, 'consolidate', False)

    if use_consolidated:
        try:
            consolidated_product = zoho_bill.consolidated_product
            item_amount = float(consolidated_product.consolidated_amount) if consolidated_product.consolidated_amount else 0
            total_amount += item_amount

            line_item = {
                "account_id": str(consolidated_product.chart_of_accounts.accountId) if consolidated_product.chart_of_accounts else str(zoho_bill.chart_of_accounts.accountId),
                "description": consolidated_product.consolidated_item_details or "Consolidated Expense Items",
                "amount": str(item_amount),
                "item_order": str(item_order),
            }
            if consolidated_product.taxes:
                line_item['tax_id'] = str(consolidated_product.taxes.taxId)
            line_items.append(line_item)
        except Exception as e:
            logger.error(f"Error accessing consolidated product, falling back to individual: {e}")
            use_consolidated = False

    if not use_consolidated:
        for item in zoho_products:
            try:
                if not item.chart_of_accounts:
                    continue
                item_amount = float(item.amount) if item.amount else 0
                total_amount += item_amount

                line_item = {
                    "account_id": str(item.chart_of_accounts.accountId),
                    "description": item.item_details or "Expense Item",
                    "amount": str(item_amount),
                    "item_order": str(item_order),
                }
                if item.taxes:
                    line_item['tax_id'] = str(item.taxes.taxId)
                line_items.append(line_item)
                item_order += 1
            except Exception as e:
                logger.error(f"Error processing expense product {item.id}: {e}")
                continue

    return line_items, total_amount


def _handle_verify_errors(errors, zoho_bill_data):
    """Map serializer errors to user-friendly messages."""
    if 'vendor' in errors:
        vendor_error = errors['vendor'][0] if errors['vendor'] else 'Unknown vendor error'
        if 'does not exist' in str(vendor_error):
            return Response({
                "detail": f"Vendor with ID {zoho_bill_data.get('vendor', 'Unknown')} does not exist. Please sync vendors from Zoho Books first.",
                "error_type": "vendor_not_found",
            }, status=status.HTTP_400_BAD_REQUEST)

    if 'chart_of_accounts' in errors:
        chart_error = errors['chart_of_accounts'][0] if errors['chart_of_accounts'] else ''
        if 'does not exist' in str(chart_error):
            return Response({
                "detail": f"Chart of Account with ID {zoho_bill_data.get('chart_of_accounts', 'Unknown')} does not exist. Please sync from Zoho Books first.",
                "error_type": "chart_of_accounts_not_found",
            }, status=status.HTTP_400_BAD_REQUEST)

    return Response(errors, status=status.HTTP_400_BAD_REQUEST)

# apps/module/zoho/vendor_views.py

import json
import logging
import os
import re
from datetime import datetime
from decimal import Decimal

import requests
from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, parser_classes
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.pagination import DefaultPagination
from apps.common.utils import (
    get_organization_from_request,
    calculate_string_similarity,
    normalize_company_name_enhanced,
)
from apps.common.services.duplicate_detection import (
    check_duplicate_bill as _check_duplicate_bill_generic,
    update_bill_duplicate_metadata,
)

from apps.common.services.pdf_processing import split_pdf_to_bills
from apps.organizations.models import Organization
from ..models import (
    ZohoCredentials,
    ZohoVendor,
    ZohoChartOfAccount,
    ZohoTaxes,
    VendorBill,
    VendorZohoBill,
    VendorZohoProduct,
    VendorZohoConsolidatedProduct,
    # Import models for all modules to support moving
    ExpenseBill,
    ExpenseZohoBill,
    ExpenseZohoProduct,
    ExpenseZohoConsolidatedProduct,
    JournalBill,
    JournalZohoBill,
    JournalZohoProduct,
    JournalZohoConsolidatedProduct,
)
from ..serializers.common import (
    AnalysisResponseSerializer,
)
from ..serializers.vendor_bills import (
    ZohoVendorBillSerializer,
    ZohoVendorBillDetailSerializer,
    VendorZohoBillSerializer,
    ZohoVendorBillMultipleUploadSerializer,
)
from .helpers import (
    get_zoho_credentials,
    refresh_zoho_access_token,
    make_zoho_api_request,
    find_zoho_vendor_ledger_enhanced,
    find_appropriate_zoho_coa_ledger,
    find_appropriate_zoho_tax_ledger,
    safe_numeric_string,
    analyze_zoho_bill,
)

logger = logging.getLogger(__name__)

# Backward-compat alias
_calculate_string_similarity = calculate_string_similarity


# ============================================================================
# Consolidation Helper Functions
# ============================================================================

def create_consolidated_vendor_product(zoho_bill, organization):
    """
    Create consolidated product entry for vendor bill when consolidate=True
    Handles tax conflicts by using most common or highest tax rate
    """
    from ..models import VendorZohoConsolidatedProduct

    products = zoho_bill.products.all()
    if not products.exists():
        return None

    # Calculate consolidated data using correct field names
    total_quantity = sum(float(p.quantity or 0) for p in products)
    total_amount = sum(float(p.amount or 0) for p in products)  # Fixed: use 'amount' not 'total'
    items_count = products.count()

    # Create detailed breakdown using correct field names
    item_details = []
    for product in products:
        item_details.append(f"• {product.item_name} (Qty: {product.quantity}, Rate: ₹{product.rate})")  # Fixed: use 'item_name' not 'name'

    consolidated_details = f"Consolidated {items_count} items:\n" + "\n".join(item_details)

    # Handle tax conflicts - use most common tax or highest rate
    tax_to_use = None
    if products.filter(taxes__isnull=False).exists():
        # Get tax usage frequency
        tax_usage = {}
        for product in products.filter(taxes__isnull=False):
            tax_id = product.taxes.id
            tax_rate = float(product.taxes.percentage or 0)
            if tax_id in tax_usage:
                tax_usage[tax_id]['count'] += 1
                tax_usage[tax_id]['amount'] += float(product.amount or 0)  # Fixed: use 'amount' not 'total'
            else:
                tax_usage[tax_id] = {
                    'tax': product.taxes,
                    'rate': tax_rate,
                    'count': 1,
                    'amount': float(product.amount or 0)  # Fixed: use 'amount' not 'total'
                }

        # Use tax with highest amount (most significant)
        if tax_usage:
            most_significant_tax = max(tax_usage.values(), key=lambda x: x['amount'])
            tax_to_use = most_significant_tax['tax']

    # Handle chart of accounts conflicts - use most common
    chart_of_accounts_to_use = None
    if products.filter(chart_of_accounts__isnull=False).exists():
        chart_usage = {}
        for product in products.filter(chart_of_accounts__isnull=False):
            chart_id = product.chart_of_accounts.id
            if chart_id in chart_usage:
                chart_usage[chart_id]['count'] += 1
                chart_usage[chart_id]['amount'] += float(product.amount or 0)  # Fixed: use 'amount' not 'total'
            else:
                chart_usage[chart_id] = {
                    'chart': product.chart_of_accounts,
                    'count': 1,
                    'amount': float(product.amount or 0)  # Fixed: use 'amount' not 'total'
                }

        # Use chart of accounts with highest amount
        if chart_usage:
            most_significant_chart = max(chart_usage.values(), key=lambda x: x['amount'])
            chart_of_accounts_to_use = most_significant_chart['chart']
            most_significant_chart = max(chart_usage.values(), key=lambda x: x['amount'])
            chart_of_accounts_to_use = most_significant_chart['chart']

    # Create or update consolidated product - with ForeignKey, we can have multiple
    # Check if there's already a consolidated product for this bill
    existing_consolidated = VendorZohoConsolidatedProduct.objects.filter(
        zohoBill=zoho_bill
    ).first()

    if existing_consolidated:
        # Update existing consolidated product
        existing_consolidated.consolidated_item_details = consolidated_details
        existing_consolidated.consolidated_amount = total_amount
        existing_consolidated.consolidated_rate = total_amount
        existing_consolidated.chart_of_accounts = chart_of_accounts_to_use
        existing_consolidated.taxes = tax_to_use
        existing_consolidated.original_items_count = items_count
        existing_consolidated.save()
        consolidated_product = existing_consolidated
    else:
        # Create new consolidated product
        consolidated_product = VendorZohoConsolidatedProduct.objects.create(
            zohoBill=zoho_bill,
            organization=organization,
            consolidated_item_name=f"Multiple items consolidated ({items_count} products)",
            consolidated_item_details=consolidated_details,
            total_quantity=1,  # Single consolidated item
            consolidated_rate=total_amount,
            consolidated_amount=total_amount,
            chart_of_accounts=chart_of_accounts_to_use,
            taxes=tax_to_use,
            original_items_count=items_count,
            consolidation_notes=f"Consolidated from {items_count} individual items. Tax strategy: highest amount. Chart strategy: highest amount."
        )

    return consolidated_product


# ============================================================================
# Helper aliases (duplicates removed – now imported from apps.common)
# ============================================================================

# Backward-compatible local name → shared implementation
_calculate_string_similarity = calculate_string_similarity


def check_duplicate_bill(bill, organization):
    """Wrapper: delegates to generic duplicate detection with VendorBill model."""
    return _check_duplicate_bill_generic(bill, organization, VendorBill)


def update_vendor_bill_duplicate_metadata(bill, duplicate_bills, max_similarity):
    """Wrapper: delegates to generic duplicate metadata updater."""
    return update_bill_duplicate_metadata(bill, duplicate_bills, max_similarity)


def validate_zoho_bill_ownership(json_data, organization):
    """Validate if the vendor bill belongs to the organization — delegates to shared service.

    Returns a rich dict with ``is_valid``, ``confidence``, ``reason``,
    and ``validation_details``.  Adds a Zoho-specific cross-check: if
    the extracted customer GST matches an existing ``ZohoVendor`` we
    lower the confidence (that GST belongs to a vendor, not the org).
    """
    from apps.common.services.ownership import validate_bill_ownership as _core

    result = _core(json_data, organization, check_field='to', bill_type='vendor bill')

    # ── Zoho-specific refinement: cross-check customer GST against vendor DB ──
    party_gst = result["validation_details"].get("extracted_party_gst", "")
    if party_gst and len(party_gst.strip()) >= 10:
        try:
            existing_vendor = ZohoVendor.objects.filter(
                organization=organization,
                gstNo=party_gst.strip(),
            ).first()
            if existing_vendor:
                # The "customer" GST actually belongs to a known vendor — suspicious
                result["confidence"] = min(result["confidence"], 20)
                result["is_valid"] = result["confidence"] >= 60
                result["validation_details"]["vendor_gst_collision"] = True
                result["validation_details"]["vendor_name"] = existing_vendor.companyName
                result["reason"] = (
                    f"GST {party_gst} belongs to existing vendor "
                    f"{existing_vendor.companyName} — low confidence"
                )
        except Exception as exc:
            logger.warning("Zoho vendor GST cross-check failed: %s", exc)

    return result


def analyze_vendor_bill_with_openai(file_content, file_extension):
    """Analyze vendor bill — delegates to shared helper."""
    return analyze_zoho_bill(file_content, file_extension, label="vendor")


def create_vendor_zoho_objects_from_analysis(bill, analyzed_data, organization):
    """
    Create VendorZohoBill and VendorZohoProduct objects from analyzed data with ownership validation.
    """
    from .bill_creation import flatten_analyzed_schema, lookup_vendor_from_analysis, parse_analysis_dates

    logger.info(f"Creating Vendor Zoho objects for bill {bill.id} with analyzed data: {analyzed_data}")

    # Step 1: Validate bill ownership
    try:
        ownership_result = validate_zoho_bill_ownership(analyzed_data, organization)
        logger.info(f"Ownership validation result: {ownership_result}")
        
        # Store ownership validation result in bill for later reference
        if hasattr(bill, 'ownership_validation_status'):
            bill.ownership_validation_status = 'valid' if ownership_result['is_valid'] else 'invalid'
        if hasattr(bill, 'ownership_validation_message'):
            bill.ownership_validation_message = ownership_result['reason']
        if hasattr(bill, 'ownership_validation_confidence'):
            bill.ownership_validation_confidence = ownership_result['confidence']

        # Save ownership validation fields if they exist
        ownership_fields = ['ownership_validation_status', 'ownership_validation_message', 'ownership_validation_confidence']
        existing_fields = []
        for field in ownership_fields:
            if hasattr(bill, field):
                existing_fields.append(field)

        if hasattr(bill, 'bill_belong_your_org'):
            bill.bill_belong_your_org = ownership_result.get('is_valid', False)
            existing_fields.append('bill_belong_your_org')

        if hasattr(bill, 'description'):
            bill.description = ownership_result.get('reason') or bill.description or ''
            existing_fields.append('description')

        if existing_fields:
            # dict.fromkeys preserves order while removing duplicates
            bill.save(update_fields=list(dict.fromkeys(existing_fields)))
            
    except Exception as e:
        logger.error(f"Ownership validation failed: {str(e)}")
        # Continue with processing even if ownership validation fails
    
    # Step 2: Parse analysis data using shared utilities
    relevant_data = flatten_analyzed_schema(analyzed_data)
    vendor, company_name, vendor_gst = lookup_vendor_from_analysis(
        relevant_data, organization, use_fallback=True,
    )
    bill_date, due_date = parse_analysis_dates(relevant_data, multi_format=True)

    # Create or update VendorZohoBill with enhanced automation
    try:
        zoho_bill, created = VendorZohoBill.objects.get_or_create(
            selectBill=bill,
            organization=organization,
            defaults={
                'vendor': vendor,
                'bill_no': relevant_data.get('invoiceNumber', ''),
                'bill_date': bill_date,
                'due_date': due_date,
                'total': safe_numeric_string(relevant_data.get('total')),
                'igst': safe_numeric_string(relevant_data.get('igst')),
                'cgst': safe_numeric_string(relevant_data.get('cgst')),
                'sgst': safe_numeric_string(relevant_data.get('sgst')),
                'discount_type': 'Percentage',
                'discount_amount': Decimal('0'),
                'adjustment_amount': Decimal('0'),
                'note': f"✅ Auto-filled from AI analysis for {company_name or 'Unknown Vendor'} | GST: {vendor_gst or 'N/A'}"[:95] + "..."
            }
        )

        if created:
            logger.info(f"✅ Created new VendorZohoBill with automation: {zoho_bill.id}")
        else:
            logger.info(f"🔄 Updating existing VendorZohoBill with enhanced data: {zoho_bill.id}")
            # Update the existing bill with new analyzed data
            zoho_bill.vendor = vendor
            zoho_bill.bill_no = relevant_data.get('invoiceNumber', zoho_bill.bill_no)
            zoho_bill.bill_date = bill_date or zoho_bill.bill_date
            zoho_bill.due_date = due_date or zoho_bill.due_date
            zoho_bill.total = safe_numeric_string(relevant_data.get('total'), zoho_bill.total)
            zoho_bill.igst = safe_numeric_string(relevant_data.get('igst'), zoho_bill.igst)
            zoho_bill.cgst = safe_numeric_string(relevant_data.get('cgst'), zoho_bill.cgst)
            zoho_bill.sgst = safe_numeric_string(relevant_data.get('sgst'), zoho_bill.sgst)
            zoho_bill.note = f"🔄 Updated from AI analysis for {company_name or 'Unknown Vendor'} | GST: {vendor_gst or 'N/A'}"[:95] + "..."
            zoho_bill.save()
            logger.info(f"✅ Updated VendorZohoBill with automation: {zoho_bill.id}")

        # Delete existing products and recreate them
        existing_products_count = zoho_bill.products.count()
        if existing_products_count > 0:
            zoho_bill.products.all().delete()
            logger.info(f"Deleted {existing_products_count} existing products")

        # ✅ Create VendorZohoProduct objects with FULL AUTOMATION
        items = relevant_data.get('items', [])
        logger.info(f"🔧 Creating {len(items)} product line items with full automation")

        created_products = []
        igst_val = float(relevant_data.get('igst', 0) or 0)
        cgst_val = float(relevant_data.get('cgst', 0) or 0) 
        sgst_val = float(relevant_data.get('sgst', 0) or 0)

        for idx, item in enumerate(items):
            try:
                rate = Decimal(item.get('price', 0) or 0)
                quantity = int(item.get('quantity', 0) or 0)
                amount = rate * quantity
                item_description = item.get('description', f'Item {idx + 1}')

                # ✅ AUTO-ASSIGN CHART OF ACCOUNTS based on item description
                try:
                    chart_of_accounts = find_appropriate_zoho_coa_ledger(organization, item_description, 'vendor')
                    logger.info(f"✅ Auto-assigned Chart of Accounts for '{item_description}': {chart_of_accounts}")
                except Exception as e:
                    chart_of_accounts = None
                    logger.warning(f"⚠️ Could not auto-assign Chart of Accounts for '{item_description}': {str(e)}")

                # ✅ AUTO-ASSIGN TAX based on GST values
                try:
                    taxes = find_appropriate_zoho_tax_ledger(organization, 'igst' if igst_val > 0 else 'cgst', igst_val or cgst_val)
                    logger.info(f"✅ Auto-assigned Tax for '{item_description}': {taxes}")
                except Exception as e:
                    taxes = None
                    logger.warning(f"⚠️ Could not auto-assign Tax for '{item_description}': {str(e)}")

                # Create product with automation
                product = VendorZohoProduct.objects.create(
                    zohoBill=zoho_bill,
                    organization=organization,
                    item_name=item_description[:100],
                    item_details=item_description[:200],
                    rate=str(rate),
                    quantity=str(quantity),
                    amount=str(amount),
                    chart_of_accounts=chart_of_accounts,  # ✅ Auto-assigned
                    taxes=taxes,  # ✅ Auto-assigned
                    itc_eligibility='eligible',  # Default to eligible
                    reverse_charge_tax_id=False  # Default to False
                )
                created_products.append(product)
                logger.info(f"✅ Created automated product {idx + 1}: {product.item_name} | CoA: {chart_of_accounts} | Tax: {taxes} | Amount: ₹{amount}")
                
            except Exception as e:
                logger.error(f"❌ Error creating automated product {idx + 1}: {str(e)}")
                continue

        logger.info(f"✅ Successfully created {len(created_products)} automated products for bill {zoho_bill.id}")

        # ✅ AUTO-CREATE CONSOLIDATED PRODUCT WITH FULL AUTOMATION
        if len(created_products) > 1:
            try:
                # Delete existing consolidated product if exists
                VendorZohoConsolidatedProduct.objects.filter(zohoBill=zoho_bill).delete()

                # Calculate consolidated data
                total_amount = sum(Decimal(str(p.amount or 0)) for p in created_products)
                items_count = len(created_products)

                # Create detailed breakdown
                item_details = []
                for product in created_products:
                    coa_name = product.chart_of_accounts.accountName if product.chart_of_accounts else 'No Account'
                    tax_name = product.taxes.taxName if product.taxes else 'No Tax'
                    item_details.append(f'• {product.item_name} (Qty: {product.quantity}, Rate: ₹{product.rate}, CoA: {coa_name}, Tax: {tax_name})')

                consolidated_details = f'✅ Auto-consolidated {items_count} items:\n' + '\n'.join(item_details)
                consolidated_name = f'Consolidated Items - {zoho_bill.bill_no or "Bill"} ({items_count} items)'

                # ✅ AUTO-ASSIGN CHART OF ACCOUNTS for consolidated product
                try:
                    consolidated_chart_of_accounts = find_appropriate_zoho_coa_ledger(organization, "consolidated vendor bill items", 'vendor')
                    logger.info(f"✅ Auto-assigned consolidated Chart of Accounts: {consolidated_chart_of_accounts}")
                except Exception as e:
                    consolidated_chart_of_accounts = None
                    logger.warning(f"⚠️ Could not auto-assign consolidated Chart of Accounts: {str(e)}")

                # ✅ AUTO-ASSIGN TAX for consolidated product
                try:
                    consolidated_taxes = find_appropriate_zoho_tax_ledger(organization, 'igst' if igst_val > 0 else 'cgst', igst_val or cgst_val)
                    logger.info(f"✅ Auto-assigned consolidated Tax: {consolidated_taxes}")
                except Exception as e:
                    consolidated_taxes = None
                    logger.warning(f"⚠️ Could not auto-assign consolidated Tax: {str(e)}")

                # Create consolidated product with full automation
                consolidated_product = VendorZohoConsolidatedProduct.objects.create(
                    zohoBill=zoho_bill,
                    organization=organization,
                    consolidated_item_name=consolidated_name,
                    consolidated_item_details=consolidated_details,
                    total_quantity=Decimal('1'),  # Always 1 for consolidated
                    consolidated_rate=total_amount,  # Total amount as rate
                    consolidated_amount=total_amount,
                    chart_of_accounts=consolidated_chart_of_accounts,  # ✅ Auto-assigned
                    taxes=consolidated_taxes,  # ✅ Auto-assigned  
                    original_items_count=items_count,
                    consolidation_notes=f'✅ Auto-created with full automation during AI analysis for {items_count} items | Total: ₹{total_amount}',
                    itc_eligibility='eligible',  # Default
                    reverse_charge_tax_id=False
                )

                logger.info(f"✅ Auto-created FULLY AUTOMATED consolidated product for bill {zoho_bill.id} | {items_count} items | ₹{total_amount} | CoA: {consolidated_chart_of_accounts} | Tax: {consolidated_taxes}")

            except Exception as e:
                logger.error(f"❌ Error creating automated consolidated product for bill {zoho_bill.id}: {str(e)}")
                # Don't raise - consolidated product creation failure shouldn't break the main flow
        else:
            logger.info(f"ℹ️ Skipping consolidated product - bill has only {len(created_products)} item(s)")

        # ✅ LOG AUTOMATION SUCCESS AND DUPLICATE DETECTION
        automation_summary = []
        automation_summary.append(f"✅ ZOHO VENDOR BILL AUTOMATION COMPLETE for Bill {zoho_bill.id}")
        automation_summary.append(f"📋 Vendor: {vendor.companyName if vendor else '⚠️ Not Found - User needs to select manually'}")
        automation_summary.append(f"📄 Invoice: {zoho_bill.bill_no}")  
        automation_summary.append(f"🏪 GST Number: {vendor_gst or 'N/A'}")
        automation_summary.append(f"📦 Products: {len(created_products)} with auto-assigned CoA & Tax")
        automation_summary.append(f"💰 Total Amount: ₹{zoho_bill.total}")
        if len(created_products) > 1:
            automation_summary.append(f"📦 Consolidated: Available with auto-assigned CoA & Tax")
        
        # Check for duplicate bills after analysis
        try:
            is_duplicate, duplicate_bills, max_similarity = check_duplicate_bill(bill, organization)
            if is_duplicate and duplicate_bills:
                update_vendor_bill_duplicate_metadata(bill, duplicate_bills, max_similarity)
                automation_summary.append(f"⚠️ DUPLICATE ALERT: {len(duplicate_bills)} similar bill(s) found ({max_similarity:.1f}% similarity)")
            else:
                automation_summary.append(f"✅ No duplicates detected - Bill is unique")
        except Exception as dup_error:
            automation_summary.append(f"⚠️ Duplicate check failed: {str(dup_error)}")
            logger.warning(f"Duplicate check failed for bill {bill.id}: {str(dup_error)}")
            
        logger.info("\n" + "\n".join(automation_summary))

        # ✅ FINAL SUCCESS CONFIRMATION   
        logger.info(f"🎉 AUTOMATION COMPLETE: Zoho vendor bill {zoho_bill.id} created with full field automation!")
        return zoho_bill

    except Exception as e:
        logger.error(f"Error creating Zoho objects for bill {bill.id}: {str(e)}")
        raise


def process_pdf_splitting_vendor(pdf_file, organization, file_type, uploaded_by):
    """Split PDF into per-page vendor bills — delegates to shared service."""
    return split_pdf_to_bills(
        pdf_file, organization, file_type, uploaded_by,
        bill_model=VendorBill, filename_prefix="BM-Vendor-Page",
    )


# ============================================================================
# Vendor Bills API Views
# ============================================================================
# ✅
@extend_schema(
    responses=ZohoVendorBillSerializer(many=True),
    tags=["Zoho Vendor Bills"],
    methods=["GET"]
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def vendor_bills_list_view(request, org_id):
    """List all vendor bills for the organization with pagination."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    bills = VendorBill.objects.filter(organization=organization)

    # Filter by status based on query parameters
    status_param = request.query_params.get('status', '').lower()
    if status_param == 'draft':
        bills = bills.filter(status='Draft')
    elif status_param == 'analysed':
        bills = bills.filter(status__in=['Analysed', 'Verified'])
    elif status_param == 'synced':
        bills = bills.filter(status='Synced')

    bills = bills.order_by('-created_at')

    # Apply pagination
    paginator = DefaultPagination()
    paginated_bills = paginator.paginate_queryset(bills, request)

    if paginated_bills is not None:
        serializer = ZohoVendorBillSerializer(paginated_bills, many=True, context={'request': request})
        return paginator.get_paginated_response(serializer.data)

    # Fallback if pagination fails
    serializer = ZohoVendorBillSerializer(bills, many=True)
    return Response({"results": serializer.data})


# ✅
@extend_schema(
    summary="Upload Vendor Bills",
    description="Upload single or multiple vendor bill files (PDF, JPG, PNG). Supports both single file and multiple file uploads with PDF splitting for multiple invoices.",
    request=ZohoVendorBillMultipleUploadSerializer,
    responses={201: ZohoVendorBillSerializer(many=True)},
    tags=['Zoho Vendor Bills']
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser])
def vendor_bill_upload_view(request, org_id):
    """Handle single or multiple vendor bill file uploads.

    Delegates to the shared ``zoho_bills_upload_base`` helper — same
    pattern the expense + journal upload views use. Consolidation
    removed ~250 lines of copy-paste (see #13 in the upload audit).
    """
    from ..tasks import enqueue_vendor_bill_analysis, split_pdf_bill_vendor
    from .bill_view_helpers import zoho_bills_upload_base

    return zoho_bills_upload_base(
        request, org_id,
        bill_model=VendorBill,
        list_serializer=ZohoVendorBillSerializer,
        upload_serializer=ZohoVendorBillMultipleUploadSerializer,
        label='vendor',
        split_pdf_fn=process_pdf_splitting_vendor,
        enqueue_analysis_fn=enqueue_vendor_bill_analysis,
        pdf_split_task_fn=split_pdf_bill_vendor,
    )


# ------------------------------------------------------------------
# Legacy inline implementation kept as ``_vendor_bill_upload_legacy``
# purely for git history / reference. Delete once the shared helper
# has soaked in production for a release.
# ------------------------------------------------------------------
def _vendor_bill_upload_legacy(request, org_id):
    """DEPRECATED — kept for reference only. Do not call."""
    files_data = []

    # Debug logging with prints (will show in gunicorn logs)
    logger.debug(f"[VENDOR DEBUG] Request data keys: {list(request.data.keys())}")
    logger.debug(f"[VENDOR DEBUG] 'files' in request.data: {'files' in request.data}")
    logger.debug(f"[VENDOR DEBUG] 'file' in request.data: {'file' in request.data}")

    # Check if files are provided as a list (multiple files)
    if 'files' in request.data:
        files_data = request.data.getlist('files') if hasattr(request.data, 'getlist') else request.data.get('files', [])
        # Ensure files_data is always a list
        if not isinstance(files_data, list):
            files_data = [files_data] if files_data else []
        logger.debug(f"[VENDOR DEBUG] Found 'files' field with {len(files_data)} file(s)")
    # Check if a single file is provided
    elif 'file' in request.data:
        single_file = request.data.get('file')
        if single_file:
            files_data = [single_file]
        logger.debug(f"[VENDOR DEBUG] Found 'file' field with {len(files_data)} file(s)")

    logger.debug(f"[VENDOR DEBUG] Total files collected: {len(files_data)}")

    # Debug: Print details about each file
    for i, f in enumerate(files_data):
        logger.debug(f"[VENDOR DEBUG] File {i+1}: {getattr(f, 'name', 'Unknown')} - Size: {getattr(f, 'size', 'Unknown')}")

    # Prepare data for serializer validation
    serializer_data = {
        'files': files_data,
        'fileType': request.data.get('fileType', 'Single Invoice/File')
    }

    logger.debug(f"[VENDOR DEBUG] Serializer data: files count = {len(serializer_data['files'])}, fileType = {serializer_data['fileType']}")

    serializer = ZohoVendorBillMultipleUploadSerializer(data=serializer_data)
    if not serializer.is_valid():
        return Response({
            'error': 'Invalid Upload Data',
            'detail': 'The uploaded file data is invalid. Please check file format and size requirements.',
            'errors': serializer.errors
        }, status=status.HTTP_400_BAD_REQUEST)

    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({
            'error': 'Organization Not Found',
            'detail': f'Organization with ID {org_id} not found or you do not have access to it. Please check the organization ID and your permissions.'
        }, status=status.HTTP_400_BAD_REQUEST)

    files = serializer.validated_data['files']
    file_type = serializer.validated_data['fileType']
    created_bills = []

    logger.debug(f"[VENDOR DEBUG] After serializer validation - files count: {len(files)}, fileType: {file_type}")

    if not files:
        return Response({
            'error': 'No Files Provided',
            'detail': 'At least one file must be provided for upload. Please select files to upload.'
        }, status=status.HTTP_400_BAD_REQUEST)

    try:
        # atomic() restored — earlier code had this commented out with
        # a "Temporarily removing atomic transaction to debug" note.
        # Without atomic, a mid-loop failure leaves partial bill rows +
        # orphan files on disk (see #3 in the upload audit).
        with transaction.atomic():
            logger.debug(f"[VENDOR DEBUG] Starting to process {len(files)} files")

            # Check for potential duplicates based on file characteristics
            upload_warnings = []

            for i, uploaded_file in enumerate(files):
                logger.debug(f"[VENDOR DEBUG] Processing file {i+1}/{len(files)}: {uploaded_file.name}")
                file_extension = uploaded_file.name.lower().split('.')[-1]

                # Check for potential file-level duplicates (same name, similar size)
                similar_files = VendorBill.objects.filter(
                    organization=organization,
                    file__isnull=False
                ).exclude(status='Draft')

                potential_duplicate_files = []
                for existing_bill in similar_files:
                    if existing_bill.file and existing_bill.file.name:
                        existing_filename = os.path.basename(existing_bill.file.name)
                        uploaded_filename = uploaded_file.name

                        # Check for exact filename match
                        if existing_filename.lower() == uploaded_filename.lower():
                            potential_duplicate_files.append({
                                'bill': existing_bill,
                                'match_type': 'exact_filename',
                                'reason': 'Same filename detected'
                            })
                        # Check for similar filename (without extension or with slight differences)
                        elif (existing_filename.lower().replace('.pdf', '').replace('.jpg', '').replace('.png', '') ==
                              uploaded_filename.lower().replace('.pdf', '').replace('.jpg', '').replace('.png', '')):
                            potential_duplicate_files.append({
                                'bill': existing_bill,
                                'match_type': 'similar_filename',
                                'reason': 'Similar filename detected'
                            })
                        # Check file size similarity (within 5% difference)
                        elif (existing_bill.file and existing_bill.file.name and
                              hasattr(existing_bill.file.storage, 'exists') and
                              existing_bill.file.storage.exists(existing_bill.file.name) and
                              hasattr(existing_bill.file, 'size') and hasattr(uploaded_file, 'size')):
                            try:
                                existing_size = existing_bill.file.size
                                uploaded_size = uploaded_file.size

                                if (existing_size > 0 and uploaded_size > 0 and
                                    abs(existing_size - uploaded_size) / max(existing_size, uploaded_size) < 0.05):
                                    potential_duplicate_files.append({
                                        'bill': existing_bill,
                                        'match_type': 'similar_size',
                                        'reason': 'Similar file size detected'
                                    })

                            except (FileNotFoundError, OSError) as e:
                                # Handle file access errors gracefully
                                logger.error(f"[VENDOR DEBUG] Error accessing file for bill {existing_bill.billmunshiName}: {str(e)}")
                                continue

                if potential_duplicate_files:
                    upload_warnings.append({
                        'uploaded_file': uploaded_file.name,
                        'potential_duplicates': len(potential_duplicate_files),
                        'warning': f'File "{uploaded_file.name}" may be a duplicate of existing bills',
                        'existing_bills': [
                            {
                                'bill_name': dup['bill'].billmunshiName,
                                'bill_id': str(dup['bill'].id),
                                'match_type': dup['match_type'],
                                'reason': dup['reason']
                            } for dup in potential_duplicate_files[:3]  # Limit to first 3 matches
                        ]
                    })

                # Handle PDF splitting for multiple invoice files
                if (file_type == 'Multiple Invoice/File' and
                        file_extension == 'pdf'):

                    logger.debug(f"[VENDOR DEBUG] Processing as PDF split for file: {uploaded_file.name}")
                    pdf_bills = process_pdf_splitting_vendor(
                        uploaded_file, organization, file_type, request.user
                    )
                    logger.debug(f"[VENDOR DEBUG] PDF splitting created {len(pdf_bills)} bills")
                    created_bills.extend(pdf_bills)
                else:
                    # Create single bill (including PDFs for single invoice type)
                    logger.debug(f"[VENDOR DEBUG] Creating single bill for file: {uploaded_file.name}")
                    bill = VendorBill.objects.create(
                        file=uploaded_file,
                        fileType=file_type,
                        organization=organization,
                        uploaded_by=request.user,
                        status='Draft'
                    )
                    logger.debug(f"[VENDOR DEBUG] Created bill: {bill.billmunshiName} (ID: {bill.id})")
                    created_bills.append(bill)

        logger.debug(f"[VENDOR DEBUG] Completed processing all files. Total bills created: {len(created_bills)}")

        # Start background processing for all created bills
        from ..tasks import enqueue_vendor_bill_analysis
        processing_jobs = []

        for bill in created_bills:
            try:
                job = enqueue_vendor_bill_analysis(str(bill.id), str(organization.id))
                bill.job_id = job.id
                bill.is_processing = True
                bill.save(update_fields=['job_id', 'is_processing'])

                processing_jobs.append({
                    'bill_id': str(bill.id),
                    'bill_name': bill.billmunshiName,
                    'job_id': job.id,
                    'status': 'queued_for_processing'
                })
                logger.info(f"Queued vendor bill {bill.billmunshiName} for background processing")
            except Exception as e:
                logger.error(f"Failed to queue vendor bill {bill.billmunshiName}: {str(e)}")
                processing_jobs.append({
                    'bill_id': str(bill.id),
                    'bill_name': bill.billmunshiName,
                    'job_id': None,
                    'status': 'failed_to_queue',
                    'error': str(e)
                })

        response_serializer = ZohoVendorBillSerializer(created_bills, many=True, context={'request': request})

        # Log the successful result
        logger.info(f"Successfully processed {len(files)} files and created {len(created_bills)} bills")

        response_data = {
            'message': f'Successfully uploaded {len(files)} file(s) and created {len(created_bills)} bill(s). Processing started in background.',
            'files_uploaded': len(files),
            'bills_created': len(created_bills),
            'bills': response_serializer.data,
            'processing_jobs': processing_jobs,
            'note': 'Bills are being processed in the background. The page will automatically refresh to show results.'
        }

        # Add file duplicate warnings if any
        if upload_warnings:
            response_data['upload_warnings'] = upload_warnings
            response_data['warning_message'] = f"📁 FILE WARNING: {len(upload_warnings)} file(s) may be duplicates based on filename/size"

        return Response(response_data, status=status.HTTP_201_CREATED)

    except Exception as e:
        logger.error(f"Error uploading vendor bills: {str(e)}")
        logger.error(f"Exception type: {type(e).__name__}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return Response({
            'error': 'File Upload Processing Failed',
            'detail': 'There was an error processing the uploaded files. This could be due to file corruption, unsupported format, or server issues.',
            'message': str(e)
        }, status=status.HTTP_400_BAD_REQUEST)


# ✅
@extend_schema(
    responses=ZohoVendorBillDetailSerializer,
    tags=["Zoho Vendor Bills"],
    methods=["GET"]
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def vendor_bill_detail_view(request, org_id, bill_id):
    """Get vendor bill details including analysis data."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        # Fetch the VendorBill without prefetch_related to avoid relationship errors
        bill = VendorBill.objects.get(id=bill_id, organization=organization)

        # Get the next bill with 'Analyzed' status
        next_bill_id = None
        analysed_bills = VendorBill.objects.filter(
            organization=organization,
            status='Analysed'
        ).exclude(id=bill_id).values_list('id', flat=True)

        if analysed_bills:
            next_bill_id = str(analysed_bills[0])  # Get the first analysed bill
            logger.info(f"Found next analysed bill: {next_bill_id}")
        else:
            logger.info("No analysed bills found for next_bill")

        # Always set next_bill on the bill object
        bill.next_bill = next_bill_id

        # Get the related VendorZohoBill if it exists
        try:
            zoho_bill = VendorZohoBill.objects.select_related(
                'vendor', 'tds_tcs_id'
            ).prefetch_related(
                'products__chart_of_accounts',
                'products__taxes',
                'consolidated_products'  # Updated to use ForeignKey relationship
            ).get(selectBill=bill, organization=organization)

            # Attach zoho_bill to the bill object for the serializer
            bill.zoho_bill = zoho_bill
        except VendorZohoBill.DoesNotExist:
            # If no VendorZohoBill exists, set it to None
            bill.zoho_bill = None

        # Serialize the data with request context for full URLs
        serializer = ZohoVendorBillDetailSerializer(bill, context={
            'request': request,
            'organization': organization
        })
        return Response(serializer.data)

    except VendorBill.DoesNotExist:
        return Response({"detail": "Vendor bill not found"}, status=status.HTTP_404_NOT_FOUND)


# ✅
@extend_schema(
    summary="Check Bill Processing Status",
    description="Check the status of background processing for a vendor bill",
    responses={200: "Bill processing status information"},
    tags=['Zoho Vendor Bills']
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def vendor_bill_processing_status(request, org_id, bill_id):
    """Check processing status of a vendor bill including background job status"""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({'error': 'Organization not found'}, status=status.HTTP_404_NOT_FOUND)

    try:
        bill = VendorBill.objects.get(id=bill_id, organization=organization)
        
        # Check if bill is currently being processed
        is_processing = getattr(bill, 'is_processing', False)
        processing_error = getattr(bill, 'processing_error', '')
        
        # Get duplicate information
        is_duplicate = getattr(bill, 'is_duplicate', False)
        duplicate_score = getattr(bill, 'duplicate_score', 0.0)
        duplicate_description = getattr(bill, 'duplicate_description', '')
        duplicate_matched_bills = getattr(bill, 'duplicate_matched_bills', [])
        
        # Get ownership validation results
        ownership_status = getattr(bill, 'ownership_validation_status', 'unknown')
        ownership_message = getattr(bill, 'ownership_validation_message', '')
        ownership_confidence = getattr(bill, 'ownership_validation_confidence', 0)
        
        # Determine overall status
        if is_processing:
            overall_status = 'processing'
        elif processing_error:
            overall_status = 'error'
        elif bill.status == 'Draft':
            if bill.analysed_data:
                overall_status = 'analyzed_pending_verification'
            else:
                overall_status = 'pending_analysis'
        elif bill.status == 'Analysed':
            overall_status = 'analyzed_ready_for_verification'
        elif bill.status == 'Verified':
            overall_status = 'verified_ready_for_sync'
        elif bill.status == 'Synced':
            overall_status = 'synced'
        else:
            overall_status = 'unknown'
        
        # Get next available action
        next_action = None
        if overall_status == 'pending_analysis':
            next_action = 'analyze'
        elif overall_status in ['analyzed_pending_verification', 'analyzed_ready_for_verification']:
            next_action = 'verify'
        elif overall_status == 'verified_ready_for_sync':
            next_action = 'sync'
        
        response_data = {
            'bill_id': str(bill.id),
            'status': bill.status,
            'overall_status': overall_status,
            'next_action': next_action,
            'is_processing': is_processing,
            'processing_error': processing_error,
            'has_analysis_data': bool(bill.analysed_data),
            'duplicate_check': {
                'is_duplicate': is_duplicate,
                'similarity_score': duplicate_score,
                'description': duplicate_description,
                'matched_bills_count': len(duplicate_matched_bills) if duplicate_matched_bills else 0,
                'matched_bills': duplicate_matched_bills[:3] if duplicate_matched_bills else []  # Limit to top 3
            },
            'ownership_validation': {
                'status': ownership_status,
                'message': ownership_message,
                'confidence': ownership_confidence
            },
            'timestamps': {
                'created_at': bill.created_at.isoformat() if bill.created_at else None,
                'updated_at': bill.update_at.isoformat() if bill.update_at else None
            },
            'file_info': {
                'file_name': bill.file.name if bill.file else None,
                'file_type': bill.fileType
            }
        }
        
        return Response(response_data, status=status.HTTP_200_OK)
        
    except VendorBill.DoesNotExist:
        return Response({'error': 'Vendor bill not found'}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        logger.error(f"Error checking bill processing status: {str(e)}")
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ✅
@extend_schema(
    responses=AnalysisResponseSerializer,
    tags=["Zoho Vendor Bills"],
    methods=["POST"]
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def vendor_bill_analyze_view(request, org_id, bill_id):
    """Analyze vendor bill using OpenAI. Changes status from 'Draft' to 'Analyzed'."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        bill = VendorBill.objects.get(id=bill_id, organization=organization)

        if bill.status != 'Draft':
            return Response({
                'error': 'Invalid Bill Status',
                'detail': f'Bill must be in "Draft" status to analyze. Current status: {bill.status}',
                'current_status': bill.status,
                'required_status': 'Draft'
            }, status=status.HTTP_400_BAD_REQUEST)

        # Read file content
        try:
            bill.file.seek(0)
            file_content = bill.file.read()
            file_extension = bill.file.name.split('.')[-1].lower()
        except Exception as e:
            logger.error(f"Error reading bill file: {e}")
            return Response({
                'error': 'File Read Error',
                'detail': 'Unable to read the bill file. The file may be corrupted or inaccessible.',
                'message': str(e)
            }, status=status.HTTP_400_BAD_REQUEST)

        bill.is_processing = True
        bill.processing_error = ""
        bill.save(update_fields=['is_processing', 'processing_error'])

        try:
            # Analyze with OpenAI
            analyzed_data = analyze_vendor_bill_with_openai(file_content, file_extension)

            # Update bill with analyzed data
            bill.analysed_data = analyzed_data
            bill.status = 'Analysed'
            bill.process = True
            bill.save()

            # Create Zoho bill and product objects from analysis
            create_vendor_zoho_objects_from_analysis(bill, analyzed_data, organization)

            # Check for duplicate bills after analysis
            is_duplicate, duplicate_bills, max_similarity = check_duplicate_bill(bill, organization)
            update_vendor_bill_duplicate_metadata(bill, duplicate_bills, max_similarity)

            response_data = {
                "detail": "Bill analyzed successfully",
                "analyzed_data": analyzed_data
            }

            # Add duplicate warnings if found
            if is_duplicate:
                duplicate_warnings = []
                for dup in duplicate_bills:
                    duplicate_warnings.append({
                        "duplicate_bill_id": str(dup['bill'].id),
                        "duplicate_bill_name": dup['bill'].billmunshiName,
                        "similarity_score": round(dup['similarity_score'], 2),
                        "match_reasons": dup['match_reasons'],
                        "invoice_number": dup['invoice_number'],
                        "vendor_name": dup['vendor_name'],
                        "total": dup['total'],
                        "date": dup['date'],
                        "status": dup['bill'].status
                    })

                response_data.update({
                    "duplicate_warning": True,
                    "duplicate_count": len(duplicate_bills),
                    "max_similarity": round(max_similarity, 2),
                    "duplicate_bills": duplicate_warnings,
                    "warning_message": f"⚠️ DUPLICATE DETECTED: Found {len(duplicate_bills)} similar bill(s) in your organization. "
                                      f"This bill appears to be {round(max_similarity, 1)}% similar to existing bills. "
                                      "Please review carefully before proceeding to avoid duplicate entries."
                })

                logger.warning(f"Duplicate bill detected for {bill.billmunshiName} - {len(duplicate_bills)} similar bills found")

            return Response(response_data)

        except Exception as analysis_error:
            bill.processing_error = str(analysis_error)
            bill.save(update_fields=['processing_error'])
            raise
        finally:
            bill.is_processing = False
            bill.save(update_fields=['is_processing'])

    except VendorBill.DoesNotExist:
        return Response({"detail": "Vendor bill not found"}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        logger.error(f"Analysis failed: {str(e)}")
        return Response(
            {"detail": f"Analysis failed: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@extend_schema(
    request=VendorZohoBillSerializer,
    responses=VendorZohoBillSerializer,
    tags=["Zoho Vendor Bills"],
    methods=["POST"]
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def vendor_bill_verify_view(request, org_id, bill_id):
    """Verify and update Zoho vendor data. Changes status from 'Analyzed' to 'Verified'."""
    logger.error("="*80)
    logger.error("[DEBUG] vendor_bill_verify_view - FUNCTION CALLED!")
    logger.error(f"[DEBUG] vendor_bill_verify_view - Request method: {request.method}")
    logger.error(f"[DEBUG] vendor_bill_verify_view - URL params - org_id: {org_id}, bill_id: {bill_id}")

    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        logger.error(f"[DEBUG] vendor_bill_verify_view - ERROR: Organization not found for org_id: {org_id}")
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        logger.error(f"[DEBUG] vendor_bill_verify_view - Inside try block, processing request data")
        logger.error(f"[DEBUG] vendor_bill_verify_view - request.data type: {type(request.data)}")
        logger.error(f"[DEBUG] vendor_bill_verify_view - request.data content: {request.data}")

        # Handle the new payload format - extract bill_id and zoho_bill data
        payload_bill_id = request.data.get('bill_id', bill_id)
        zoho_bill_data = request.data.get('zoho_bill', request.data)

        logger.error(f"[DEBUG] vendor_bill_verify_view - Starting verification for bill_id: {payload_bill_id}")
        logger.error(f"[DEBUG] vendor_bill_verify_view - Organization: {organization.name if organization else 'None'}")
        logger.error(f"[DEBUG] vendor_bill_verify_view - Received zoho_bill_data keys: {list(zoho_bill_data.keys()) if zoho_bill_data else 'None'}")

        # Debug vendor data in the payload
        vendor_data = zoho_bill_data.get('vendor')
        if vendor_data:
            logger.error(f"[DEBUG] vendor_bill_verify_view - Vendor data in payload: {vendor_data}")
            logger.error(f"[DEBUG] vendor_bill_verify_view - Vendor data type: {type(vendor_data)}")

            # Validate vendor exists before proceeding
            try:
                from ..models import ZohoVendor
                vendor_obj = ZohoVendor.objects.get(id=vendor_data, organization=organization)
                logger.error(f"[DEBUG] vendor_bill_verify_view - Found vendor in database: {vendor_obj.companyName} (ID: {vendor_obj.id})")
            except ZohoVendor.DoesNotExist:
                logger.error(f"[DEBUG] vendor_bill_verify_view - ERROR: Vendor {vendor_data} does not exist in organization {organization.name}")
                return Response({
                    'error': 'Vendor Not Found',
                    'detail': f'Vendor with ID {vendor_data} does not exist in this organization. Please sync vendors from Zoho Books first.',
                    'vendor_id': vendor_data,
                    'solution': 'Sync vendors from Zoho Books or select a different vendor'
                }, status=status.HTTP_400_BAD_REQUEST)
            except Exception as vendor_check_error:
                logger.error(f"[DEBUG] vendor_bill_verify_view - Error checking vendor: {vendor_check_error}")
        else:
            logger.error(f"[DEBUG] vendor_bill_verify_view - No vendor data found in payload")

        # Use the bill_id from payload if provided, otherwise use URL parameter
        bill = VendorBill.objects.get(id=payload_bill_id, organization=organization)
        logger.error(f"[DEBUG] vendor_bill_verify_view - Found VendorBill: {bill.id}, status: {bill.status}")

        if bill.status not in ['Analysed', 'Verified']:
            return Response({
                'error': 'Invalid Bill Status',
                'detail': f'Bill must be in "Analysed" or "Verified" status to save. Current status: {bill.status}',
                'current_status': bill.status,
                'required_status': ['Analysed', 'Verified']
            }, status=status.HTTP_400_BAD_REQUEST)

        # Get existing VendorZohoBill
        logger.error(f"[DEBUG] vendor_bill_verify_view - Attempting to find VendorZohoBill for bill: {bill.id}, org: {organization.id}")
        try:
            zoho_bill = VendorZohoBill.objects.get(selectBill=bill, organization=organization)
            logger.error(f"[DEBUG] vendor_bill_verify_view - Found existing VendorZohoBill: {zoho_bill.id}")
            logger.error(f"[DEBUG] vendor_bill_verify_view - Current vendor in zoho_bill: {zoho_bill.vendor}")
            if zoho_bill.vendor:
                logger.error(f"[DEBUG] vendor_bill_verify_view - Current vendor details: ID={zoho_bill.vendor.id}, Name={zoho_bill.vendor.companyName}, ContactID={zoho_bill.vendor.contactId}")
            else:
                logger.error(f"[DEBUG] vendor_bill_verify_view - No vendor currently assigned to zoho_bill")
        except VendorZohoBill.DoesNotExist:
            logger.error(f"[DEBUG] vendor_bill_verify_view - VendorZohoBill not found for bill {bill.id}")
            return Response({
                'error': 'Analysis Data Not Found',
                'detail': 'No analyzed vendor data found for this bill. Please analyze the bill first before verification.',
                'bill_id': str(bill.id),
                'solution': 'Use the analyze endpoint to process the bill first'
            }, status=status.HTTP_400_BAD_REQUEST)
        except Exception as zoho_bill_error:
            logger.error(f"[DEBUG] vendor_bill_verify_view - Unexpected error getting VendorZohoBill: {zoho_bill_error}")
            logger.error(f"[DEBUG] vendor_bill_verify_view - Error type: {type(zoho_bill_error).__name__}")
            import traceback
            logger.error(f"[DEBUG] vendor_bill_verify_view - Traceback: {traceback.format_exc()}")
            return Response(
                {"detail": f"Error retrieving vendor data: {str(zoho_bill_error)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        with transaction.atomic():
            # Use partial=True for POST as we're updating existing data
            logger.error(f"[DEBUG] vendor_bill_verify_view - Creating serializer with partial=True")
            logger.error(f"[DEBUG] vendor_bill_verify_view - Serializer data being passed: {zoho_bill_data}")

            # Pass organization in context for proper vendor queryset scoping
            serializer = VendorZohoBillSerializer(
                zoho_bill, 
                data=zoho_bill_data, 
                partial=True,
                context={'organization': organization}
            )

            if serializer.is_valid():
                logger.error(f"[DEBUG] vendor_bill_verify_view - Serializer is valid, proceeding to save")
                logger.error(f"[DEBUG] vendor_bill_verify_view - Validated data keys: {list(serializer.validated_data.keys())}")

                # Log critical fields before save
                vendor_in_validated = serializer.validated_data.get('vendor')
                if vendor_in_validated:
                    logger.error(f"[DEBUG] vendor_bill_verify_view - Vendor in validated_data: {vendor_in_validated.companyName} (ID: {vendor_in_validated.id})")
                else:
                    logger.error(f"[DEBUG] vendor_bill_verify_view - No vendor in validated_data")

                # Log other critical fields
                for field in ['bill_no', 'bill_date', 'due_date', 'total', 'discount_amount', 'adjustment_amount']:
                    if field in serializer.validated_data:
                        logger.error(f"[DEBUG] vendor_bill_verify_view - {field}: {serializer.validated_data[field]}")

                # Save the bill data
                try:
                    updated_bill = serializer.save()
                    logger.error(f"[DEBUG] vendor_bill_verify_view - Bill saved successfully: ID={updated_bill.id}")
                except Exception as save_error:
                    logger.error(f"[DEBUG] vendor_bill_verify_view - ERROR saving bill: {save_error}")
                    import traceback
                    logger.error(f"[DEBUG] vendor_bill_verify_view - Save traceback: {traceback.format_exc()}")
                    return Response({
                        'error': 'Bill Save Failed',
                        'detail': f'Failed to save bill data: {str(save_error)}',
                        'solution': 'Check that all required fields are provided correctly'
                    }, status=status.HTTP_400_BAD_REQUEST)

                # Verify critical fields were saved correctly
                logger.error(f"[DEBUG] vendor_bill_verify_view - Verifying saved data:")
                logger.error(f"  - Vendor: {updated_bill.vendor.companyName if updated_bill.vendor else 'NOT SET'}")
                logger.error(f"  - Bill No: {updated_bill.bill_no}")
                logger.error(f"  - Bill Date: {updated_bill.bill_date}")
                logger.error(f"  - Due Date: {updated_bill.due_date}")
                logger.error(f"  - Total: {updated_bill.total}")
                logger.error(f"  - Discount: {updated_bill.discount_amount} ({updated_bill.discount_type})")
                logger.error(f"  - Adjustment: {updated_bill.adjustment_amount}")

                # Warn if critical fields are missing
                if not updated_bill.vendor:
                    logger.error(f"[DEBUG] vendor_bill_verify_view - WARNING: Vendor not saved! Check if vendor ID was in request.")
                if not updated_bill.bill_no:
                    logger.error(f"[DEBUG] vendor_bill_verify_view - WARNING: Bill number not saved!")

                logger.error(f"[DEBUG] vendor_bill_verify_view - Complete updated bill: {updated_bill}")
                
                # Handle products update if provided
                products_data = zoho_bill_data.get('products')
                if products_data is not None:
                    logger.error(f"[DEBUG] vendor_bill_verify_view - Processing {len(products_data)} products from request")
                    
                    # Get all existing products for this bill
                    existing_products = {str(product.id): product for product in updated_bill.products.all()}
                    logger.error(f"[DEBUG] vendor_bill_verify_view - Found {len(existing_products)} existing products in database")

                    # Track which products were processed (to keep)
                    processed_product_ids = set()
                    created_count = 0
                    updated_count = 0

                    # Process each product from the request
                    for idx, product_data in enumerate(products_data):
                        logger.error(f"[DEBUG] vendor_bill_verify_view - Processing product {idx + 1}: {product_data}")
                        
                        # Validate that item_details is present (required field)
                        if not product_data.get('item_details'):
                            logger.warning(f"[DEBUG] vendor_bill_verify_view - Skipping product {idx + 1}: missing item_details")
                            continue

                        product_id = product_data.get('id')
                        logger.error(f"[DEBUG] vendor_bill_verify_view - Product ID from request: {product_id}")

                        # Prepare product data for creation/update
                        product_fields = {
                            'item_name': product_data.get('item_name'),
                            'item_details': product_data.get('item_details'),
                            'chart_of_accounts_id': product_data.get('chart_of_accounts'),
                            'taxes_id': product_data.get('taxes'),
                            'reverse_charge_tax_id': product_data.get('reverse_charge_tax_id', False),
                            'itc_eligibility': product_data.get('itc_eligibility', 'eligible'),
                            'rate': product_data.get('rate'),
                            'quantity': product_data.get('quantity'),
                            'amount': product_data.get('amount'),
                        }

                        # Remove None values to avoid overwriting with null
                        product_fields = {k: v for k, v in product_fields.items() if v is not None}
                        logger.error(f"[DEBUG] vendor_bill_verify_view - Product fields to save: {product_fields}")

                        # Check if this is an existing product (has valid ID in database)
                        if product_id and str(product_id) in existing_products:
                            # UPDATE existing product
                            existing_product = existing_products[str(product_id)]
                            logger.error(f"[DEBUG] vendor_bill_verify_view - Updating existing product: {product_id}")
                            
                            # Update each field
                            for field, value in product_fields.items():
                                setattr(existing_product, field, value)
                            
                            existing_product.save()
                            processed_product_ids.add(str(product_id))
                            updated_count += 1
                            logger.error(f"[DEBUG] vendor_bill_verify_view - Successfully updated product {product_id}")
                        else:
                            # CREATE new product (either no ID provided or ID doesn't exist)
                            logger.error(f"[DEBUG] vendor_bill_verify_view - Creating new product (ID: {product_id})")
                            
                            try:
                                new_product = VendorZohoProduct.objects.create(
                                    zohoBill=updated_bill,
                                    organization=organization,
                                    **product_fields
                                )
                                processed_product_ids.add(str(new_product.id))
                                created_count += 1
                                logger.error(f"[DEBUG] vendor_bill_verify_view - Successfully created new product with ID: {new_product.id}")
                            except Exception as create_error:
                                logger.error(f"[DEBUG] vendor_bill_verify_view - Error creating product: {create_error}")
                                # Continue processing other products even if one fails
                                continue

                    # DELETE products that were NOT in the request (removed by user)
                    products_to_delete = set(existing_products.keys()) - processed_product_ids
                    deleted_count = len(products_to_delete)
                    
                    if products_to_delete:
                        logger.error(f"[DEBUG] vendor_bill_verify_view - Deleting {deleted_count} products not in request: {products_to_delete}")
                        deleted = VendorZohoProduct.objects.filter(
                            id__in=products_to_delete,
                            zohoBill=updated_bill
                        ).delete()
                        logger.error(f"[DEBUG] vendor_bill_verify_view - Deleted {deleted[0]} product records")

                    logger.error(f"[DEBUG] vendor_bill_verify_view - Product operations summary:")
                    logger.error(f"  - Created: {created_count} products")
                    logger.error(f"  - Updated: {updated_count} products")
                    logger.error(f"  - Deleted: {deleted_count} products")

                # 🔄 Handle consolidate_prod array from frontend
                consolidate_prod_data = zoho_bill_data.get('consolidate_prod', [])
                if consolidate_prod_data:
                    logger.error(f"[DEBUG] vendor_bill_verify_view - Processing consolidate_prod array with {len(consolidate_prod_data)} items")

                    try:
                        # 🔄 FIRST: Clear existing consolidated products to prevent duplicates
                        existing_consolidated = updated_bill.consolidated_products.all()
                        if existing_consolidated.exists():
                            existing_count = existing_consolidated.count()
                            existing_consolidated.delete()
                            logger.error(f"[DEBUG] vendor_bill_verify_view - Deleted {existing_count} existing consolidated products before creating new ones")

                        # Handle consolidated product creation (since we cleared existing ones, always create new)
                        for idx, consolidated_data in enumerate(consolidate_prod_data):
                            logger.error(f"[DEBUG] vendor_bill_verify_view - Creating consolidated product {idx + 1}: {consolidated_data.get('item_name', 'Unnamed')}")

                            # Create new consolidated product - ForeignKey allows multiple
                            consolidated_product = VendorZohoConsolidatedProduct.objects.create(
                                zohoBill=updated_bill,
                                organization=organization,
                                consolidated_item_name=consolidated_data.get('item_name', 'Consolidated Product'),
                                consolidated_item_details=consolidated_data.get('item_details', 'Consolidated product from verification'),
                                consolidated_rate=consolidated_data.get('rate', 0),
                                total_quantity=consolidated_data.get('quantity', 1),
                                consolidated_amount=consolidated_data.get('amount', 0),
                                chart_of_accounts_id=consolidated_data.get('chart_of_accounts'),
                                taxes_id=consolidated_data.get('taxes'),
                                itc_eligibility=consolidated_data.get('itc_eligibility', 'eligible'),
                                reverse_charge_tax_id=consolidated_data.get('reverse_charge_tax_id', False),
                                original_items_count=1,
                                consolidation_notes='Created from frontend verification'
                            )
                            logger.error(f"[DEBUG] vendor_bill_verify_view - Created new consolidated product {consolidated_product.id}")

                    except Exception as consolidate_error:
                        logger.error(f"[DEBUG] vendor_bill_verify_view - Error processing consolidate_prod array: {consolidate_error}")
                        # Don't fail the entire request, just log the error

                # Handle consolidation logic
                consolidate_flag = zoho_bill_data.get('consolidate', False)
                if consolidate_flag != updated_bill.consolidate:
                    logger.info(f"Consolidation setting changed from {updated_bill.consolidate} to {consolidate_flag}")
                    updated_bill.consolidate = consolidate_flag
                    updated_bill.save()

                # Create or update consolidated product if consolidation is enabled
                # BUT ONLY if consolidate_prod array was NOT provided (to avoid duplicates)
                if updated_bill.consolidate and not consolidate_prod_data:
                    logger.info(f"Creating/updating consolidated product for bill {updated_bill.id} (auto-consolidation)")
                    try:
                        consolidated_product = create_consolidated_vendor_product(updated_bill, organization)
                        if consolidated_product:
                            logger.info(f"Successfully created/updated consolidated product: {consolidated_product.id}")
                        else:
                            logger.warning(f"Failed to create consolidated product for bill {updated_bill.id}")
                    except Exception as consolidation_error:
                        logger.error(f"Error during consolidation: {str(consolidation_error)}")
                        # Don't fail the entire request for consolidation errors
                elif updated_bill.consolidate and consolidate_prod_data:
                    logger.info(f"Skipping auto-consolidation for bill {updated_bill.id} - consolidate_prod array was provided")
                # NOTE: We don't delete consolidated products when consolidate=false
                # The consolidate flag only controls VIEW/SYNC behavior, not data persistence
                # Users should be able to switch between individual and consolidated views without losing data

                # Log summary only if products were processed
                if products_data is not None:
                    logger.error(f"[DEBUG] vendor_bill_verify_view - Product processing summary:")
                    logger.error(f"  - Created: {created_count}")
                    logger.error(f"  - Updated: {updated_count}")
                    logger.error(f"  - Deleted: {deleted_count}")
                    logger.error(f"  - Total processed: {len(processed_product_ids)}")
                else:
                    logger.error(f"[DEBUG] vendor_bill_verify_view - No products data in request, skipping product update")

                # Validate bill has required data before marking as Verified
                validation_errors = []
                if not updated_bill.vendor:
                    validation_errors.append("Vendor is required")
                if not updated_bill.bill_no:
                    validation_errors.append("Bill number is required")
                if not updated_bill.bill_date:
                    validation_errors.append("Bill date is required")
                
                # Check if bill has at least one product (individual OR consolidated)
                product_count = updated_bill.products.count()
                consolidated_count = updated_bill.consolidated_products.count()
                total_line_items = product_count + consolidated_count

                if total_line_items == 0:
                    validation_errors.append("At least one product line item or consolidated product is required")

                logger.error(f"[DEBUG] vendor_bill_verify_view - Line item counts: Individual={product_count}, Consolidated={consolidated_count}, Total={total_line_items}")

                if validation_errors:
                    logger.error(f"[DEBUG] vendor_bill_verify_view - Validation failed: {validation_errors}")
                    return Response({
                        'error': 'Validation Failed',
                        'detail': 'Bill cannot be verified due to missing required information',
                        'validation_errors': validation_errors,
                        'solution': 'Please provide all required fields and at least one product line item'
                    }, status=status.HTTP_400_BAD_REQUEST)

                # Update bill status to Verified
                logger.error(f"[DEBUG] vendor_bill_verify_view - Updating bill status from '{bill.status}' to 'Verified'")
                bill.status = 'Verified'
                bill.save()
                logger.error(f"[DEBUG] vendor_bill_verify_view - Bill status updated successfully to '{bill.status}'")

                # Refresh the bill to get latest data with relationships
                updated_bill.refresh_from_db()
                
                # Prepare response with full bill data
                response_data = VendorZohoBillSerializer(
                    updated_bill, 
                    context={'organization': organization, 'request': request}
                ).data
                
                # Log final verification summary
                logger.error(f"[DEBUG] vendor_bill_verify_view - Verification completed successfully")
                logger.error(f"[DEBUG] vendor_bill_verify_view - Final summary:")
                logger.error(f"  - Vendor: {response_data.get('vendor')}")
                logger.error(f"  - Individual products count: {len(response_data.get('products', []))}")
                logger.error(f"  - Consolidated products count: {len(response_data.get('consolidate_prod', []))}")
                logger.error(f"  - Bill total: {response_data.get('total')}")
                logger.error(f"  - Status: Verified")

                return Response(response_data)

            else:
                logger.error(f"[DEBUG] vendor_bill_verify_view - Serializer validation FAILED")
                logger.error(f"[DEBUG] vendor_bill_verify_view - Serializer errors: {serializer.errors}")

                # Check if vendor-related errors exist and provide helpful message
                if 'vendor' in serializer.errors:
                    logger.error(f"[DEBUG] vendor_bill_verify_view - Vendor-specific errors: {serializer.errors['vendor']}")
                    vendor_error_detail = serializer.errors['vendor'][0] if serializer.errors['vendor'] else 'Unknown vendor error'

                    # Check if it's a "does not exist" error
                    if 'does not exist' in str(vendor_error_detail):
                        vendor_id = zoho_bill_data.get('vendor', 'Unknown')
                        custom_error = {
                            "detail": f"Vendor with ID {vendor_id} does not exist in the database. Please sync vendors from Zoho Books first or select a different vendor.",
                            "vendor_id": vendor_id,
                            "error_type": "vendor_not_found"
                        }
                        return Response(custom_error, status=status.HTTP_400_BAD_REQUEST)

                return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    except VendorBill.DoesNotExist:
        logger.error(f"[DEBUG] vendor_bill_verify_view - ERROR: VendorBill not found with ID: {payload_bill_id}, org: {organization.id if organization else 'None'}")
        return Response({"detail": "Vendor bill not found"}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        logger.error(f"[DEBUG] vendor_bill_verify_view - UNEXPECTED ERROR: {str(e)}")
        logger.error(f"[DEBUG] vendor_bill_verify_view - Error type: {type(e).__name__}")
        import traceback
        logger.error(f"[DEBUG] vendor_bill_verify_view - Traceback: {traceback.format_exc()}")
        return Response(
            {"detail": f"Verification failed: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@extend_schema(
    responses={"200": {"detail": "Bill synced to Zoho successfully"}},
    tags=["Zoho Vendor Bills"],
    methods=["POST"]
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def vendor_bill_sync_view(request, org_id, bill_id):
    """Sync verified vendor bill to Zoho Books. Changes status to 'Synced'."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        bill = VendorBill.objects.get(id=bill_id, organization=organization)

        if bill.status != 'Verified':
            return Response({
                'error': 'Invalid Bill Status',
                'detail': f'Bill must be in "Verified" status to sync. Current status: {bill.status}',
                'current_status': bill.status,
                'required_status': 'Verified'
            }, status=status.HTTP_400_BAD_REQUEST)

        # Get Zoho vendor data
        try:
            zoho_bill = VendorZohoBill.objects.get(selectBill=bill, organization=organization)
        except VendorZohoBill.DoesNotExist:
            return Response({
                'error': 'Zoho Data Not Found',
                'detail': 'Zoho vendor data not found for this bill. Please verify the bill first.',
                'bill_id': str(bill.id),
                'solution': 'Use the verify endpoint to process the bill first'
            }, status=status.HTTP_400_BAD_REQUEST)

        # Get Zoho credentials
        try:
            current_token = ZohoCredentials.objects.get(organization=organization)
        except ZohoCredentials.DoesNotExist:
            return Response({
                'error': 'Zoho Credentials Not Found',
                'detail': 'Zoho Books credentials are not configured for this organization. Please configure Zoho credentials first.',
                'solution': 'Add Zoho Books credentials in the organization settings'
            }, status=status.HTTP_400_BAD_REQUEST)

        # Get products
        zoho_products = zoho_bill.products.all()
        if not zoho_products.exists():
            return Response({
                'error': 'No Products Found',
                'detail': 'No line items found for this bill. At least one product line item is required to sync.',
                'bill_id': str(bill.id),
                'solution': 'Add product line items to the bill before syncing'
            }, status=status.HTTP_400_BAD_REQUEST)

        # Get vendor
        if not zoho_bill.vendor:
            return Response({
                'error': 'Vendor Not Specified',
                'detail': 'No vendor is associated with this bill. Please assign a vendor before syncing.',
                'bill_id': str(bill.id),
                'solution': 'Update the bill to include vendor information'
            }, status=status.HTTP_400_BAD_REQUEST)

        # Prepare data for Zoho API (Vendor Bill)
        bill_date_str = zoho_bill.bill_date.strftime('%Y-%m-%d') if zoho_bill.bill_date else None
        due_date_str = zoho_bill.due_date.strftime('%Y-%m-%d') if zoho_bill.due_date else None
        tax_choice = getattr(zoho_bill, 'is_tax', 'No')

        bill_data = {
            "vendor_id": zoho_bill.vendor.contactId,
            "bill_number": zoho_bill.bill_no,
            "gst_no": zoho_bill.vendor.gstNo,
            "date": bill_date_str,
            "line_items": []
        }
        
        # Use consolidated or individual line items based on consolidate flag
        if zoho_bill.consolidate:
            logger.info(f"Syncing bill {bill.id} with CONSOLIDATED line items")

            # Use consolidated product if available
            try:
                consolidated_product = zoho_bill.consolidated_product
                logger.info(f"Using existing consolidated product: {consolidated_product.id}")

                consolidated_item = {
                    "account_id": consolidated_product.chart_of_accounts.accountId if consolidated_product.chart_of_accounts else None,
                    "name": consolidated_product.consolidated_item_name or "Consolidated Items",
                    "description": consolidated_product.consolidated_item_details[:500] if consolidated_product.consolidated_item_details else "Multiple items consolidated",
                    "rate": float(consolidated_product.consolidated_rate or 0),
                    "quantity": float(consolidated_product.total_quantity or 1),
                    "unit": "unit",
                    "item_total": float(consolidated_product.consolidated_amount or 0)
                }

                # Add tax information if available
                if consolidated_product.taxes and consolidated_product.taxes.taxId:
                    consolidated_item["tax_id"] = consolidated_product.taxes.taxId

                # Add ITC information
                if consolidated_product.itc_eligibility:
                    consolidated_item["input_tax_credit"] = consolidated_product.itc_eligibility

                bill_data["line_items"].append(consolidated_item)

                logger.info(f"Added consolidated line item: {consolidated_item['name']} (Amount: ₹{consolidated_item['item_total']})")

            except VendorZohoConsolidatedProduct.DoesNotExist:
                # Fallback: create consolidated data on-the-fly
                logger.warning(f"No consolidated product found, creating consolidated data on-the-fly")

                total_amount = sum(float(p.total or 0) for p in zoho_products)
                items_count = zoho_products.count()

                # Use most common or highest-value tax
                tax_id = None
                chart_account_id = None

                if zoho_products.filter(taxes__isnull=False).exists():
                    # Get tax with highest amount
                    tax_amounts = {}
                    for product in zoho_products.filter(taxes__isnull=False):
                        tax_id_key = product.taxes.taxId
                        amount = float(product.total or 0)
                        if tax_id_key in tax_amounts:
                            tax_amounts[tax_id_key] += amount
                        else:
                            tax_amounts[tax_id_key] = amount

                    if tax_amounts:
                        highest_tax = max(tax_amounts, key=tax_amounts.get)
                        tax_id = highest_tax

                # Use most common chart of accounts
                if zoho_products.filter(chart_of_accounts__isnull=False).exists():
                    chart_amounts = {}
                    for product in zoho_products.filter(chart_of_accounts__isnull=False):
                        chart_id_key = product.chart_of_accounts.accountId
                        amount = float(product.total or 0)
                        if chart_id_key in chart_amounts:
                            chart_amounts[chart_id_key] += amount
                        else:
                            chart_amounts[chart_id_key] = amount

                    if chart_amounts:
                        highest_chart = max(chart_amounts, key=chart_amounts.get)
                        chart_account_id = highest_chart

                consolidated_item = {
                    "account_id": chart_account_id,
                    "name": f"Multiple items consolidated ({items_count} products)",
                    "description": f"Consolidated from {items_count} individual items - Total: ₹{total_amount}",
                    "rate": total_amount,
                    "quantity": 1,
                    "unit": "unit",
                    "item_total": total_amount
                }

                if tax_id:
                    consolidated_item["tax_id"] = tax_id

                bill_data["line_items"].append(consolidated_item)

                logger.info(f"Added on-the-fly consolidated item: {items_count} products, Total: ₹{total_amount}")

        else:
            logger.info(f"Syncing bill {bill.id} with INDIVIDUAL line items")

            # Use individual products as before
            for item in zoho_products:
                line_item = {
                    "account_id": item.chart_of_accounts.accountId if item.chart_of_accounts else None,
                    "name": item.name,
                    "description": item.name,
                    "rate": float(item.rate or 0),
                    "quantity": float(item.quantity or 0),
                    "unit": "unit",
                    "item_total": float(item.total or 0)
                }

                # Add tax information
                if item.taxes and item.taxes.taxId:
                    line_item["tax_id"] = item.taxes.taxId

                # Add ITC information
                if item.itc_eligibility:
                    line_item["input_tax_credit"] = item.itc_eligibility

                bill_data["line_items"].append(line_item)

            logger.info(f"Added {len(bill_data['line_items'])} individual line items")

        # Add due date if provided
        if due_date_str:
            bill_data['due_date'] = due_date_str
        
        # Add discount information if provided
        if zoho_bill.discount and zoho_bill.discount > 0:
            # Set discount type - always entity level for bill-level discounts
            bill_data['discount_type'] = 'entity_level'
            bill_data['discount_amount'] = float(zoho_bill.discount_amount)
            
            # Handle percentage vs INR discount
            if zoho_bill.discount_type == 'Percentage':
                # Percentage discount: send as "5.00%" format (user-entered value)
                bill_data['discount'] = f"{float(zoho_bill.discount):.2f}%"
                bill_data['is_discount_before_tax'] = True
            elif zoho_bill.discount_type == 'INR':
                # INR (flat amount) discount: send as number (user-entered value)
                bill_data['discount'] = float(zoho_bill.discount)
                bill_data['is_discount_before_tax'] = False
            
            # Add discount account if specified (required for proper accounting)
            if zoho_bill.discount_account and hasattr(zoho_bill.discount_account, 'accountId'):
                bill_data['discount_account_id'] = str(zoho_bill.discount_account.accountId)
        
        # Add adjustment amount if provided
        if zoho_bill.adjustment_amount and zoho_bill.adjustment_amount != 0:
            bill_data['adjustment'] = float(zoho_bill.adjustment_amount)
            # Use custom adjustment description if provided, otherwise use default
            bill_data['adjustment_description'] = zoho_bill.adjustment_description if zoho_bill.adjustment_description else 'Bill adjustment'

        # Add TDS/TCS if applicable
        if hasattr(zoho_bill, 'tds_tcs_id') and zoho_bill.tds_tcs_id:
            if tax_choice == 'TDS':
                bill_data['tds_tax_id'] = str(zoho_bill.tds_tcs_id.taxId)
            elif tax_choice == 'TCS':
                bill_data['tcs_tax_id'] = str(zoho_bill.tds_tcs_id.taxId)


        if not bill_data["line_items"]:
            return Response({
                'error': 'No Valid Line Items',
                'detail': 'No valid line items found for syncing. All line items must have a chart of account assigned.',
                'bill_id': str(bill.id),
                'solution': 'Ensure each product has a chart of account and tax information'
            }, status=status.HTTP_400_BAD_REQUEST)

        # Sync to Zoho Books
        url = f"https://www.zohoapis.in/books/v3/bills?organization_id={current_token.organisationId}"
        payload = json.dumps(bill_data)
        headers = {
            'Authorization': f'Zoho-oauthtoken {current_token.accessToken}',
            'Content-Type': 'application/json'
        }

        try:
            response = requests.post(url, headers=headers, data=payload)

            # Handle token refresh if needed
            if response.status_code == 401:
                new_access_token = refresh_zoho_access_token(current_token)
                if new_access_token:
                    headers['Authorization'] = f'Zoho-oauthtoken {new_access_token}'
                    response = requests.post(url, headers=headers, data=payload)

            if response.status_code == 201:
                # Update bill status
                bill.status = 'Synced'
                bill.save()

                response_data = response.json()
                return Response({
                    "detail": "Bill synced to Zoho successfully",
                    "zoho_bill_id": response_data.get('bill', {}).get('bill_id')
                })
            else:
                response_json = response.json() if response.content else {}
                error_message = response_json.get("message", "Failed to send data to Zoho Books")
                logger.error(f"Zoho sync failed: {response.status_code} - {error_message}")
                return Response({
                    'error': 'Zoho Sync Failed',
                    'detail': f'Failed to sync bill to Zoho Books. {error_message}',
                    'status_code': response.status_code,
                    'zoho_message': error_message
                }, status=status.HTTP_400_BAD_REQUEST)

        except requests.RequestException as e:
            logger.error(f"Network error during Zoho sync: {str(e)}")
            return Response(
                {"detail": f"Network error: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    except VendorBill.DoesNotExist:
        return Response({"detail": "Vendor bill not found"}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        logger.error(f"Sync failed: {str(e)}")
        return Response(
            {"detail": f"Sync failed: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


# ✅
@extend_schema(
    responses={"200": {"detail": "Vendor bill deleted successfully"}},
    tags=["Zoho Vendor Bills"],
    methods=["DELETE"]
)
@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def vendor_bill_delete_view(request, org_id, bill_id):
    """Delete a vendor bill and its associated file."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        bill = VendorBill.objects.get(id=bill_id, organization=organization)

        # Delete the file from storage if it exists
        if bill.file:
            try:
                file_path = os.path.join(settings.MEDIA_ROOT, str(bill.file))
                if os.path.exists(file_path):
                    os.remove(file_path)
            except Exception as e:
                logger.warning(f"Could not delete file {bill.file}: {str(e)}")

        # Delete the bill record from the database
        bill.delete()

        return Response({
            "detail": "Vendor bill and associated file deleted successfully"
        })

    except VendorBill.DoesNotExist:
        return Response({"detail": "Vendor bill not found"}, status=status.HTTP_404_NOT_FOUND)


# ============================================================================
# Bill Moving Between Modules Functionality
# ============================================================================

@extend_schema(
    summary="Move Bills Between Modules",
    description="Move multiple bills from one module to another (vendor ↔ expense ↔ journal). Preserves analysis data to save LLM tokens.",
    request={
        'type': 'object',
        'properties': {
            'from': {
                'type': 'string',
                'enum': ['vendor', 'expense', 'journal'],
                'description': 'Source module'
            },
            'to': {
                'type': 'string',
                'enum': ['vendor', 'expense', 'journal'],
                'description': 'Destination module'
            },
            'bill_ids': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': 'Array of bill IDs to move'
            }
        },
        'required': ['from', 'to', 'bill_ids']
    },
    responses={
        200: {
            'type': 'object',
            'properties': {
                'message': {'type': 'string'},
                'total_bills': {'type': 'integer'},
                'successful_moves': {'type': 'integer'},
                'failed_moves': {'type': 'integer'},
                'moved_bills': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'original_bill_id': {'type': 'string'},
                            'new_bill_id': {'type': 'string'},
                            'status': {'type': 'string'}
                        }
                    }
                },
                'from_module': {'type': 'string'},
                'to_module': {'type': 'string'},
                'preserved_analysis': {'type': 'boolean'},
                'errors': {'type': 'array'}
            }
        },
        400: {'description': 'Invalid request or bill status'},
        404: {'description': 'Bills not found'}
    },
    tags=['Zoho Bill Management']
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def move_bill_between_modules_view(request, org_id):
    """
    Move multiple bills between vendor/expense/journal modules while preserving analysis data
    Only works for Draft/Analysed status bills to prevent data corruption
    """
    # Create function-local logger to avoid scope issues
    logger = logging.getLogger(__name__)
    
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    # Validate request data
    from_module = request.data.get('from', '').lower()
    to_module = request.data.get('to', '').lower()
    bill_ids = request.data.get('bill_ids', [])

    valid_modules = ['vendor', 'expense', 'journal']

    if not from_module or not to_module:
        return Response({
            'error': 'Missing Parameters',
            'detail': 'Both "from" and "to" parameters are required',
            'valid_modules': valid_modules
        }, status=status.HTTP_400_BAD_REQUEST)

    if from_module not in valid_modules or to_module not in valid_modules:
        return Response({
            'error': 'Invalid Module',
            'detail': f'Modules must be one of: {valid_modules}',
            'received': {'from': from_module, 'to': to_module}
        }, status=status.HTTP_400_BAD_REQUEST)

    if from_module == to_module:
        return Response({
            'error': 'Same Module',
            'detail': 'Source and destination modules cannot be the same',
            'modules': {'from': from_module, 'to': to_module}
        }, status=status.HTTP_400_BAD_REQUEST)

    if not bill_ids or not isinstance(bill_ids, list):
        return Response({
            'error': 'Missing Bill IDs',
            'detail': 'bill_ids must be provided as an array of bill IDs',
            'example': {'bill_ids': ['bill-id-1', 'bill-id-2']}
        }, status=status.HTTP_400_BAD_REQUEST)

    # Track results
    moved_bills = []
    errors = []
    successful_moves = 0
    failed_moves = 0

    for bill_id in bill_ids:
        try:
            with transaction.atomic():
                # Get source bill and zoho bill based on from_module
                source_bill = None
                source_zoho_bill = None

                if from_module == 'vendor':
                    try:
                        source_bill = VendorBill.objects.get(id=bill_id, organization=organization)
                        try:
                            source_zoho_bill = VendorZohoBill.objects.prefetch_related(
                                'products', 'consolidated_products'
                            ).get(selectBill=source_bill, organization=organization)
                        except VendorZohoBill.DoesNotExist:
                            source_zoho_bill = None
                    except VendorBill.DoesNotExist:
                        errors.append({
                            'bill_id': bill_id,
                            'error': f'Vendor bill with ID {bill_id} not found'
                        })
                        failed_moves += 1
                        continue

                elif from_module == 'expense':
                    try:
                        source_bill = ExpenseBill.objects.get(id=bill_id, organization=organization)
                        try:
                            source_zoho_bill = ExpenseZohoBill.objects.prefetch_related(
                                'products', 'consolidated_products'
                            ).get(selectBill=source_bill, organization=organization)
                        except ExpenseZohoBill.DoesNotExist:
                            source_zoho_bill = None
                    except ExpenseBill.DoesNotExist:
                        errors.append({
                            'bill_id': bill_id,
                            'error': f'Expense bill with ID {bill_id} not found'
                        })
                        failed_moves += 1
                        continue

                elif from_module == 'journal':
                    try:
                        source_bill = JournalBill.objects.get(id=bill_id, organization=organization)
                        try:
                            source_zoho_bill = JournalZohoBill.objects.prefetch_related(
                                'products', 'consolidated_products'
                            ).get(selectBill=source_bill, organization=organization)
                        except JournalZohoBill.DoesNotExist:
                            source_zoho_bill = None
                    except JournalBill.DoesNotExist:
                        errors.append({
                            'bill_id': bill_id,
                            'error': f'Journal bill with ID {bill_id} not found'
                        })
                        failed_moves += 1
                        continue

                # Check bill status - only allow Draft or Analysed
                if source_bill.status not in ['Draft', 'Analysed']:
                    errors.append({
                        'bill_id': bill_id,
                        'error': f'Bill must be in Draft or Analysed status to move. Current status: {source_bill.status}'
                    })
                    failed_moves += 1
                    continue

                # Create new bill in destination module
                new_bill = None
                new_zoho_bill = None

                # Common bill data to transfer
                common_data = {
                    'organization': organization,
                    'uploaded_by': source_bill.uploaded_by,
                    'fileType': source_bill.fileType,
                    'status': source_bill.status,
                    'process': source_bill.process,
                    'analysed_data': source_bill.analysed_data,
                    'file': source_bill.file  # Copy file reference
                }

                # Create bill based on destination module
                if to_module == 'vendor':
                    new_bill = VendorBill.objects.create(**common_data)

                    # Transfer Zoho bill data if exists
                    if source_zoho_bill:
                        # Create common zoho bill data
                        zoho_common_data = {
                            'selectBill': new_bill,
                            'organization': organization,
                            'bill_no': str(getattr(source_zoho_bill, 'bill_no', ''))[:50],
                            'bill_date': getattr(source_zoho_bill, 'bill_date', None),
                            'due_date': getattr(source_zoho_bill, 'due_date', None),
                            'total': str(getattr(source_zoho_bill, 'total', 0))[:50],
                            'igst': str(getattr(source_zoho_bill, 'igst', 0))[:50],
                            'cgst': str(getattr(source_zoho_bill, 'cgst', 0))[:50],
                            'sgst': str(getattr(source_zoho_bill, 'sgst', 0))[:50],
                            'discount_type': getattr(source_zoho_bill, 'discount_type', 'Percentage'),
                            'discount_amount': getattr(source_zoho_bill, 'discount_amount', 0),
                            'adjustment_amount': getattr(source_zoho_bill, 'adjustment_amount', 0),
                            'note': (f"Moved from {from_module} - " + getattr(source_zoho_bill, 'note', ''))[:100],
                            'consolidate': getattr(source_zoho_bill, 'consolidate', False)
                        }

                        new_zoho_bill = VendorZohoBill.objects.create(**zoho_common_data)

                elif to_module == 'expense':
                    new_bill = ExpenseBill.objects.create(**common_data)

                    if source_zoho_bill:
                        zoho_common_data = {
                            'selectBill': new_bill,
                            'organization': organization,
                            'bill_no': str(getattr(source_zoho_bill, 'bill_no', ''))[:50],
                            'bill_date': getattr(source_zoho_bill, 'bill_date', None),
                            'due_date': getattr(source_zoho_bill, 'due_date', None),
                            'total': str(getattr(source_zoho_bill, 'total', 0))[:50],
                            'igst': str(getattr(source_zoho_bill, 'igst', 0))[:50],
                            'cgst': str(getattr(source_zoho_bill, 'cgst', 0))[:50],
                            'sgst': str(getattr(source_zoho_bill, 'sgst', 0))[:50],
                            'note': (f"Moved from {from_module} - " + getattr(source_zoho_bill, 'note', ''))[:100],
                            'consolidate': getattr(source_zoho_bill, 'consolidate', False)
                        }

                        new_zoho_bill = ExpenseZohoBill.objects.create(**zoho_common_data)

                elif to_module == 'journal':
                    new_bill = JournalBill.objects.create(**common_data)

                    if source_zoho_bill:
                        zoho_common_data = {
                            'selectBill': new_bill,
                            'organization': organization,
                            'bill_no': str(getattr(source_zoho_bill, 'bill_no', ''))[:50],
                            'bill_date': getattr(source_zoho_bill, 'bill_date', None),
                            'due_date': getattr(source_zoho_bill, 'due_date', None),
                            'total': str(getattr(source_zoho_bill, 'total', 0))[:50],
                            'igst': str(getattr(source_zoho_bill, 'igst', 0))[:50],
                            'cgst': str(getattr(source_zoho_bill, 'cgst', 0))[:50],
                            'sgst': str(getattr(source_zoho_bill, 'sgst', 0))[:50],
                            'note': (f"Moved from {from_module} - " + getattr(source_zoho_bill, 'note', ''))[:100],
                            'consolidate': getattr(source_zoho_bill, 'consolidate', False)
                        }

                        new_zoho_bill = JournalZohoBill.objects.create(**zoho_common_data)

                # Transfer products/line items if they exist and bill was analysed
                if source_zoho_bill and source_bill.status == 'Analysed' and new_zoho_bill:
                    try:
                        # Transfer individual products
                        source_products = source_zoho_bill.products.all() if hasattr(source_zoho_bill, 'products') else []
                        for product in source_products:
                            if to_module == 'vendor':
                                VendorZohoProduct.objects.create(
                                    zohoBill=new_zoho_bill,
                                    organization=organization,
                                    item_name=str(getattr(product, 'item_name', '') or getattr(product, 'item_details', ''))[:1000],
                                    item_details=str(getattr(product, 'item_details', ''))[:2000],
                                    rate=str(getattr(product, 'rate', 0))[:50],
                                    quantity=str(getattr(product, 'quantity', 0))[:50],
                                    amount=str(getattr(product, 'amount', 0))[:50],
                                    chart_of_accounts=getattr(product, 'chart_of_accounts', None),
                                    taxes=getattr(product, 'taxes', None),
                                    itc_eligibility=str(getattr(product, 'itc_eligibility', 'eligible'))[:100],
                                    reverse_charge_tax_id=getattr(product, 'reverse_charge_tax_id', False)
                                )
                            elif to_module == 'expense':
                                ExpenseZohoProduct.objects.create(
                                    zohoBill=new_zoho_bill,
                                    organization=organization,
                                    item_details=str(getattr(product, 'item_details', ''))[:2000],
                                    amount=str(getattr(product, 'amount', 0))[:50],
                                    chart_of_accounts=getattr(product, 'chart_of_accounts', None),
                                    taxes=getattr(product, 'taxes', None)
                                )
                            elif to_module == 'journal':
                                # Journal has different structure - create debit/credit entries
                                JournalZohoProduct.objects.create(
                                    zohoBill=new_zoho_bill,
                                    organization=organization,
                                    item_details=str(getattr(product, 'item_details', ''))[:2000],
                                    amount=str(getattr(product, 'amount', 0))[:50],
                                    chart_of_accounts=getattr(product, 'chart_of_accounts', None),
                                    debit_or_credit='debit'  # Default to debit
                                )

                        # Transfer consolidated products if they exist
                        source_consolidated = source_zoho_bill.consolidated_products.all() if hasattr(source_zoho_bill, 'consolidated_products') else []
                        for consolidated in source_consolidated:
                            if to_module == 'vendor':
                                VendorZohoConsolidatedProduct.objects.create(
                                    zohoBill=new_zoho_bill,
                                    organization=organization,
                                    consolidated_item_name=str(getattr(consolidated, 'consolidated_item_name', '') or 'Consolidated Items')[:500],
                                    consolidated_item_details=str(getattr(consolidated, 'consolidated_item_details', '')),
                                    total_quantity=getattr(consolidated, 'total_quantity', 1),
                                    consolidated_rate=getattr(consolidated, 'consolidated_rate', 0),
                                    consolidated_amount=getattr(consolidated, 'consolidated_amount', 0),
                                    chart_of_accounts=getattr(consolidated, 'chart_of_accounts', None),
                                    taxes=getattr(consolidated, 'taxes', None),
                                    itc_eligibility=str(getattr(consolidated, 'itc_eligibility', 'eligible'))[:100],
                                    reverse_charge_tax_id=getattr(consolidated, 'reverse_charge_tax_id', False),
                                    original_items_count=getattr(consolidated, 'original_items_count', None) or getattr(consolidated, 'original_entries_count', 1),
                                    consolidation_notes=f"Moved from {from_module} module"
                                )
                            elif to_module == 'expense':
                                ExpenseZohoConsolidatedProduct.objects.create(
                                    zohoBill=new_zoho_bill,
                                    organization=organization,
                                    consolidated_item_details=str(getattr(consolidated, 'consolidated_item_details', '')),
                                    consolidated_amount=getattr(consolidated, 'consolidated_amount', 0),
                                    chart_of_accounts=getattr(consolidated, 'chart_of_accounts', None),
                                    taxes=getattr(consolidated, 'taxes', None),
                                    original_entries_count=getattr(consolidated, 'original_items_count', None) or getattr(consolidated, 'original_entries_count', 1),
                                    consolidation_notes=f"Moved from {from_module} module"
                                )
                            elif to_module == 'journal':
                                JournalZohoConsolidatedProduct.objects.create(
                                    zohoBill=new_zoho_bill,
                                    organization=organization,
                                    consolidated_item_details=str(getattr(consolidated, 'consolidated_item_details', '')),
                                    consolidated_amount=getattr(consolidated, 'consolidated_amount', 0),
                                    chart_of_accounts=getattr(consolidated, 'chart_of_accounts', None),
                                    debit_or_credit=getattr(consolidated, 'debit_or_credit', 'debit'),
                                    original_entries_count=getattr(consolidated, 'original_items_count', None) or getattr(consolidated, 'original_entries_count', 1),
                                    consolidation_notes=f"Moved from {from_module} module"
                                )

                    except Exception as product_transfer_error:
                        logger.warning(f"Failed to transfer products for bill {bill_id}: {product_transfer_error}")
                        # Don't fail the entire move operation, just log the warning

                # Delete source bill and related data
                if source_zoho_bill:
                    # Delete products and consolidated products (cascade should handle this, but being explicit)
                    if hasattr(source_zoho_bill, 'products'):
                        source_zoho_bill.products.all().delete()
                    if hasattr(source_zoho_bill, 'consolidated_products'):
                        source_zoho_bill.consolidated_products.all().delete()
                    source_zoho_bill.delete()

                source_bill.delete()

                # Track successful move
                moved_bills.append({
                    'original_bill_id': str(bill_id),
                    'new_bill_id': str(new_bill.id),
                    'status': 'success'
                })
                successful_moves += 1

                logger.info(f"Successfully moved bill {bill_id} from {from_module} to {to_module} module")

        except Exception as e:
            logger.error(f"Error moving bill {bill_id}: {str(e)}")
            errors.append({
                'bill_id': bill_id,
                'error': f'Failed to move bill: {str(e)}'
            })
            failed_moves += 1

    # Prepare response
    total_bills = len(bill_ids)

    # If all bills failed to move, return error response
    if failed_moves > 0 and successful_moves == 0:
        return Response({
            'error': 'Move Failed',
            'message': f'Failed to move all {failed_moves} bill(s)',
            'total_bills': total_bills,
            'successful_moves': successful_moves,
            'failed_moves': failed_moves,
            'moved_bills': moved_bills,
            'from_module': from_module,
            'to_module': to_module,
            'errors': errors
        }, status=status.HTTP_400_BAD_REQUEST)

    # Return success response (even if partial failures)
    return Response({
        'message': f'Batch move completed: {successful_moves} successful, {failed_moves} failed',
        'total_bills': total_bills,
        'successful_moves': successful_moves,
        'failed_moves': failed_moves,
        'moved_bills': moved_bills,
        'from_module': from_module,
        'to_module': to_module,
        'preserved_analysis': successful_moves > 0,  # Indicates if LLM re-analysis was avoided
        'llm_tokens_saved': successful_moves > 0,
        'errors': errors
    })


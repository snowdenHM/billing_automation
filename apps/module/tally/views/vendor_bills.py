# apps/module/tally/vendor_views_functional.py

import json
import logging
import os
import random
from datetime import datetime
from io import BytesIO

from PyPDF2 import PdfReader
from django.conf import settings
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
    get_organization_from_request,
    calculate_string_similarity,
    safe_get_nested,
    parse_bill_date,
    normalize_company_name,
    calculate_gst_rate,
    normalize_product_gst,
)
from apps.common.converters import safe_float_convert, safe_int_convert
from apps.common.services.duplicate_detection import check_duplicate_bill, update_bill_duplicate_metadata
from apps.common.services.pdf_processing import split_pdf_to_bills
from apps.common.services.bill_analysis import analyze_bill_file, get_vendor_bill_prompt
from apps.organizations.models import Organization
from ..models import (
    TallyVendorBill,
    TallyVendorAnalyzedBill,
    TallyVendorAnalyzedProduct,
    TallyVendorConsolidatedProduct,
    # Import expense models for bill moving
    TallyExpenseBill,
    TallyExpenseAnalyzedBill,
    TallyExpenseAnalyzedProduct,
    TallyExpenseConsolidatedProduct,
    Ledger,
    ParentLedger,
    TallyConfig,
    StockItem
)
from ..serializers import (
    TallyVendorBillSerializer,
    TallyVendorAnalyzedBillSerializer,
    VendorBillUploadSerializer,
    BillAnalysisRequestSerializer,
    BillVerificationSerializer,
    BillSyncRequestSerializer,
    BillSyncResponseSerializer
)

# OpenAI Client – no longer used directly; AI analysis is handled by common services
logger = logging.getLogger(__name__)


# ============================================================================
# Helper Functions (duplicates removed – now imported from apps.common)
# Thin wrappers kept for backward-compatible call-sites
# ============================================================================

def check_duplicate_tally_vendor_bill(bill, organization):
    """Wrapper: delegates to apps.common.services.duplicate_detection.check_duplicate_bill."""
    return check_duplicate_bill(bill, organization, TallyVendorBill)


def analyze_bill_with_ai(bill, organization):
    """Analyze bill using OpenAI API — delegates to shared service."""
    logger.info(f"Starting AI analysis for bill {bill.id}, file: {bill.file.name}")

    try:
        json_data = analyze_bill_file(bill.file.path, bill.file.name, get_vendor_bill_prompt())
        logger.info(f"AI analysis successful for bill {bill.id}")
    except Exception as e:
        logger.error(f"AI analysis failed for bill {bill.id}: {e}")
        return {'success': False, 'error': str(e), 'analyzed_bill': None}

    try:
        analyzed_bill = process_analysis_data(bill, json_data, organization)
        return {'success': True, 'analyzed_bill': analyzed_bill, 'error': None}
    except Exception as e:
        logger.error(f"Error processing analysis data for bill {bill.id}: {e}")
        return {'success': False, 'error': f"Error processing analysis data: {str(e)}", 'analyzed_bill': None}


def validate_bill_ownership(json_data, organization):
    """Validate if the bill belongs to the organization — delegates to shared service.

    Checks the ``to`` field (bill recipient = our organization) against the
    organization. The ``from`` field is the vendor who issued the bill, not us.
    """
    from apps.common.services.ownership import validate_bill_ownership_simple
    return validate_bill_ownership_simple(
        json_data, organization, check_field='to', allow_empty=True,
        bill_type='vendor bill',
    )


def process_analysis_data(bill, json_data, organization):
    """Process AI extracted data and create analyzed bill"""
    try:
        # Log the raw JSON data for debugging
        logger.warning(f"📄 Raw JSON data from OpenAI: {json.dumps(json_data, indent=2)}")

        # Validate bill ownership
        bill_belongs_to_org, ownership_description = validate_bill_ownership(json_data, organization)
        
        # Update bill ownership fields
        bill.bill_belong_your_org = bill_belongs_to_org
        bill.description = ownership_description
        
        logger.info(f"Bill ownership validation: {bill_belongs_to_org}, Description: {ownership_description}")

        # Extract relevant data with robust error handling
        relevant_data = {}

        # Handle different JSON response formats from OpenAI
        if isinstance(json_data, dict):
            # Check if this is a fallback response due to parsing error
            if "error" in json_data and json_data.get("error") == "json_parse_failed":
                logger.warning("Processing fallback JSON data due to parsing error")
                relevant_data = json_data.get("fallback_data", {})
            elif "properties" in json_data:
                # Handle schema format - extract from properties with safe access
                try:
                    relevant_data = {
                        "invoiceNumber": safe_get_nested(json_data, ["properties", "invoiceNumber", "const"], ""),
                        "dateIssued": safe_get_nested(json_data, ["properties", "dateIssued", "const"], ""),
                        "dueDate": safe_get_nested(json_data, ["properties", "dueDate", "const"], ""),
                        "from": safe_get_nested(json_data, ["properties", "from", "properties"], {}),
                        "to": safe_get_nested(json_data, ["properties", "to", "properties"], {}),
                        "items": extract_items_from_properties(json_data),
                        "total": safe_get_nested(json_data, ["properties", "total", "const"], 0),
                        "igst": safe_get_nested(json_data, ["properties", "igst", "const"], 0),
                        "cgst": safe_get_nested(json_data, ["properties", "cgst", "const"], 0),
                        "sgst": safe_get_nested(json_data, ["properties", "sgst", "const"], 0),
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

        # Save analyzed data to bill with ownership information
        bill.analysed_data = relevant_data
        bill.save(update_fields=['analysed_data', 'bill_belong_your_org', 'description'])

        # Extract required fields with safe access
        invoice_number = str(relevant_data.get('invoiceNumber', '')).strip()
        date_issued = str(relevant_data.get('dateIssued', ''))
        due_date_str = str(relevant_data.get('dueDate', ''))

        # Handle 'from' field safely
        from_data = relevant_data.get('from', {})
        if isinstance(from_data, dict):
            company_name = str(from_data.get('name', '')).strip()
            vendor_gst = str(from_data.get('gst_number', '')).strip()
            vendor_address = str(from_data.get('address', '')).strip()
            
            # If GST number is empty, try to extract from address
            if not vendor_gst and vendor_address:
                logger.warning(f"🔍 GST field empty, searching in address: {vendor_address}")
                import re
                # Enhanced GST number patterns for Indian format (2 digits + 10 alphanumeric + 1 digit + 2 characters)
                gst_patterns = [
                    r'\b\d{2}[A-Z]{5}\d{4}[A-Z]\d[A-Z]\d\b',  # Standard GST format
                    r'GST[:\s]*(\d{2}[A-Z0-9]{13})',  # GST: followed by number
                    r'GSTIN[:\s]*(\d{2}[A-Z0-9]{13})',  # GSTIN: followed by number
                    r'Tax[\s]*ID[:\s]*(\d{2}[A-Z0-9]{13})',  # Tax ID: followed by number
                    r'\b(\d{2}[A-Z]{4}\d{5}[A-Z]\d[A-Z]\d)\b',  # Another common format
                ]
                
                for pattern in gst_patterns:
                    matches = re.findall(pattern, vendor_address.upper())
                    if matches:
                        vendor_gst = matches[0] if isinstance(matches[0], str) else matches[0]
                        logger.warning(f"✅ Extracted GST from address: {vendor_gst}")
                        break
        else:
            company_name = str(from_data).strip()
            vendor_gst = ''
            vendor_address = ''

        # Log extracted vendor information
        logger.warning(f"📊 Extracted vendor info - Company: '{company_name}', GST: '{vendor_gst}', Address: '{vendor_address[:100]}{'...' if len(vendor_address) > 100 else ''}'")
        
        # Parse date with multiple format support
        bill_date = parse_bill_date(date_issued)
        due_date = parse_bill_date(due_date_str) if due_date_str else None

        # Find vendor ledger using both name and GST number
        vendor = find_vendor_ledger(company_name, organization, vendor_gst)
        
        # Log vendor finding result
        if vendor:
            logger.warning(f"✅ Successfully found and assigned vendor: {vendor.name} (ID: {vendor.id}, GST: {getattr(vendor, 'gst_in', 'None')})")
        else:
            logger.error(f"❌ No vendor found for Company: '{company_name}', GST: '{vendor_gst}'")
            logger.warning("⚠️  This will create an analyzed bill without vendor assignment")

        # Continue with bill processing regardless of vendor status
        logger.warning(f"🔄 Continuing with bill processing - Company: '{company_name}'")

        # Determine GST type with safe conversion
        logger.warning(f"📋 Starting GST values extraction from relevant_data...")
        igst_val = safe_float_convert(relevant_data.get('igst', 0))
        cgst_val = safe_float_convert(relevant_data.get('cgst', 0))
        sgst_val = safe_float_convert(relevant_data.get('sgst', 0))

        # Log GST values for debugging
        logger.warning(f"💰 GST Values extracted - IGST: {igst_val}, CGST: {cgst_val}, SGST: {sgst_val}")

        if igst_val > 0:
            gst_type = TallyVendorAnalyzedBill.GSTType.IGST
        elif cgst_val > 0 or sgst_val > 0:
            gst_type = TallyVendorAnalyzedBill.GSTType.CGST_SGST
        else:
            gst_type = TallyVendorAnalyzedBill.GSTType.UNKNOWN

        logger.warning(f"🏷️  Determined GST Type: {gst_type}")

        # 🏛️ FIND APPROPRIATE TAX LEDGERS FOR BILL-LEVEL GST VALUES
        logger.warning(f"🏛️ [NEW BILL] Finding bill-level tax ledgers - IGST: {igst_val}, CGST: {cgst_val}, SGST: {sgst_val}")
        
        igst_tax_ledger = None
        cgst_tax_ledger = None
        sgst_tax_ledger = None
        
        if igst_val > 0:
            igst_tax_ledger = find_appropriate_tax_ledger(organization, igst_val, 0, 0, ledger_type='bill')
            logger.warning(f"🎯 [NEW BILL] IGST Tax Ledger: {igst_tax_ledger.name if igst_tax_ledger else 'None'}")
        
        if cgst_val > 0:
            cgst_tax_ledger = find_appropriate_tax_ledger(organization, 0, cgst_val, 0, ledger_type='bill')
            logger.warning(f"🎯 [NEW BILL] CGST Tax Ledger: {cgst_tax_ledger.name if cgst_tax_ledger else 'None'}")
        
        if sgst_val > 0:
            sgst_tax_ledger = find_appropriate_tax_ledger(organization, 0, 0, sgst_val, ledger_type='bill')
            logger.warning(f"🎯 [NEW BILL] SGST Tax Ledger: {sgst_tax_ledger.name if sgst_tax_ledger else 'None'}")

        # Create analyzed bill
        logger.warning(f"🏗️  Starting analyzed bill creation...")
        with transaction.atomic():
            # Round decimal values to 2 decimal places to avoid validation errors
            from decimal import Decimal, ROUND_HALF_UP

            total_val = safe_float_convert(relevant_data.get('total', 0))
            discount_val = safe_float_convert(relevant_data.get('discount', 0))
            # New extraction fields — captures printed round_off / cess /
            # freight from invoices that explicitly show them. Bills that
            # don't print these still get round_off auto-computed later
            # via ``analyzed_bill.compute_round_off()``.
            round_off_val = safe_float_convert(relevant_data.get('round_off', 0))
            cess_val     = safe_float_convert(relevant_data.get('cess', 0))
            freight_val  = safe_float_convert(relevant_data.get('freight', 0))
            igst_rounded = Decimal(str(igst_val)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            cgst_rounded = Decimal(str(cgst_val)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            sgst_rounded = Decimal(str(sgst_val)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            total_rounded = Decimal(str(total_val)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            discount_rounded = Decimal(str(discount_val)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            round_off_rounded = Decimal(str(round_off_val)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            cess_rounded     = Decimal(str(cess_val)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            freight_rounded  = Decimal(str(freight_val)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            logger.warning(f"💵 Bill amounts - Total: {total_rounded}, IGST: {igst_rounded}, CGST: {cgst_rounded}, SGST: {sgst_rounded}")

            # ``get_or_create`` is idempotent under the double-analysis
            # race (sync analyze button + async RQ job). ``defaults`` are
            # only used when creating; if a row already exists we take
            # the existing one and update the tax fields below so the
            # newer analysis wins on the values that matter.
            analyzed_bill, _created = TallyVendorAnalyzedBill.objects.get_or_create(
                selected_bill=bill,
                defaults={
                    "vendor": vendor,
                    "bill_no": invoice_number,
                    "bill_date": bill_date,
                    "due_date": due_date,
                    "igst": igst_rounded,
                    "cgst": cgst_rounded,
                    "sgst": sgst_rounded,
                    "igst_taxes": igst_tax_ledger,
                    "cgst_taxes": cgst_tax_ledger,
                    "sgst_taxes": sgst_tax_ledger,
                    "discount": discount_rounded,
                    "round_off": round_off_rounded,
                    "cess": cess_rounded,
                    "freight": freight_rounded,
                    "total": total_rounded,
                    "note": "AI Analyzed Bill",
                    "organization": organization,
                    "gst_type": gst_type,
                },
            )
            if not _created:
                # Second analyzer arrived after the first — overwrite the
                # header fields with the new values (last-write-wins).
                analyzed_bill.vendor = vendor
                analyzed_bill.bill_no = invoice_number
                analyzed_bill.bill_date = bill_date
                analyzed_bill.due_date = due_date
                analyzed_bill.igst = igst_rounded
                analyzed_bill.cgst = cgst_rounded
                analyzed_bill.sgst = sgst_rounded
                analyzed_bill.igst_taxes = igst_tax_ledger
                analyzed_bill.cgst_taxes = cgst_tax_ledger
                analyzed_bill.sgst_taxes = sgst_tax_ledger
                analyzed_bill.discount = discount_rounded
                analyzed_bill.round_off = round_off_rounded
                analyzed_bill.cess = cess_rounded
                analyzed_bill.freight = freight_rounded
                analyzed_bill.total = total_rounded
                analyzed_bill.gst_type = gst_type
                analyzed_bill.save()

            logger.warning(f"✅ Created TallyVendorAnalyzedBill with ID: {analyzed_bill.id}")
            logger.warning(f"✅ [NEW BILL] Saved analyzed bill with tax ledgers - IGST: {analyzed_bill.igst_taxes}, CGST: {analyzed_bill.cgst_taxes}, SGST: {analyzed_bill.sgst_taxes}")

            # Create analyzed products with safe item extraction and tax ledger automation
            product_instances = []
            items = relevant_data.get('items', [])
            
            # Debug log for items extraction
            logger.warning(f"📝 Items extraction - Found {len(items) if isinstance(items, list) else 0} items: {items}")
            
            if isinstance(items, list):
                logger.warning(f"🔄 Processing {len(items)} items for product creation...")
                for idx, item in enumerate(items, 1):
                    if isinstance(item, dict):
                        # Handle decimal precision for product amounts
                        price_val = safe_float_convert(item.get('price', 0))
                        quantity_val = safe_int_convert(item.get('quantity', 0))
                        amount_val = price_val * quantity_val

                        # Round to 2 decimal places
                        price_rounded = Decimal(str(price_val)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                        amount_rounded = Decimal(str(amount_val)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

                        # Debug log for each item
                        logger.warning(f"📋 Item {idx}: '{item.get('description', 'No description')}' - Price: {price_val}, Qty: {quantity_val}, Amount: {amount_val}")

                        # 🧮 CALCULATE PROPORTIONAL GST FOR THIS ITEM
                        # Calculate this item's share of total GST based on its amount
                        total_taxable_amount = total_val - (igst_val + cgst_val + sgst_val)
                        if total_taxable_amount > 0 and amount_val > 0:
                            # Proportional GST calculation
                            item_proportion = amount_val / total_taxable_amount
                            item_igst = igst_val * item_proportion
                            item_cgst = cgst_val * item_proportion
                            item_sgst = sgst_val * item_proportion
                        else:
                            # Fallback: distribute equally among items if calculation fails
                            num_items = len([i for i in items if isinstance(i, dict) and safe_float_convert(i.get('price', 0)) > 0])
                            if num_items > 0:
                                item_igst = igst_val / num_items
                                item_cgst = cgst_val / num_items
                                item_sgst = sgst_val / num_items
                            else:
                                item_igst = item_cgst = item_sgst = 0
                        
                        # Round GST values to 2 decimal places
                        item_igst_rounded = Decimal(str(item_igst)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                        item_cgst_rounded = Decimal(str(item_cgst)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                        item_sgst_rounded = Decimal(str(item_sgst)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                        
                        logger.warning(f"🧮 Item {idx} proportional GST - IGST: {item_igst_rounded}, CGST: {item_cgst_rounded}, SGST: {item_sgst_rounded}")

                        # � AUTO-ASSIGN LINE ITEM LEDGER FROM CHART OF ACCOUNTS (Purchase Accounts)
                        logger.warning(f"🏛️  Finding line item ledger for product '{item.get('description', 'No desc')[:30]}...'")
                        item_ledger = find_appropriate_tax_ledger(organization, float(item_igst_rounded), float(item_cgst_rounded), float(item_sgst_rounded), ledger_type='product')
                        
                        # Determine GST rate from tax amounts for this specific item
                        gst_rate = calculate_gst_rate(amount_val, float(item_igst_rounded), float(item_cgst_rounded), float(item_sgst_rounded))
                        
                        # Format GST rate to match choices using normalization
                        formatted_gst_rate = normalize_product_gst(gst_rate)

                        product = TallyVendorAnalyzedProduct(
                            vendor_bill_analyzed=analyzed_bill,
                            item_details=str(item.get('description', '')),
                            price=price_rounded,
                            quantity=quantity_val,
                            amount=amount_rounded,
                            taxes=item_ledger,  # 🎯 Line item ledger from chart_of_accounts_parents (Purchase Accounts)
                            product_gst=formatted_gst_rate,   # 🎯 Auto-assigned GST rate with % format
                            igst=item_igst_rounded,  # 🎯 Individual item IGST
                            cgst=item_cgst_rounded,  # 🎯 Individual item CGST
                            sgst=item_sgst_rounded,  # 🎯 Individual item SGST
                            organization=organization
                        )
                        product_instances.append(product)
                        
                        logger.warning(f"📦 Created product {idx}: '{item.get('description', '')[:50]}...' | Line Item Ledger: '{item_ledger.name if item_ledger else 'None'}' (Parent: {item_ledger.parent.parent if item_ledger else 'N/A'}) | GST: {formatted_gst_rate}")
                    else:
                        logger.warning(f"❌ Item {idx} is not a dict: {item}")
            else:
                logger.warning(f"❌ Items is not a list: {type(items)} - {items}")

            if product_instances:
                logger.warning(f"💾 Saving {len(product_instances)} product instances to database...")
                TallyVendorAnalyzedProduct.objects.bulk_create(product_instances)
                logger.warning(f"✅ Successfully created {len(product_instances)} products for bill {analyzed_bill.id}")

                # ✅ AUTO-CREATE CONSOLIDATED PRODUCT FOR MULTI-ITEM BILLS
                if len(product_instances) > 1:
                    try:
                        # Delete existing consolidated product if exists
                        TallyVendorConsolidatedProduct.objects.filter(vendor_bill_analyzed=analyzed_bill).delete()

                        # Calculate consolidated data with proper decimal handling
                        total_amount = sum(p.amount for p in product_instances)
                        items_count = len(product_instances)

                        # Round values to 2 decimal places
                        total_rounded = Decimal(str(total_amount)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

                        # Create detailed breakdown
                        item_details = []
                        for product in product_instances:
                            item_details.append(f'• {product.item_details} (Qty: {product.quantity}, Rate: ₹{product.price})')

                        consolidated_details = f'Consolidated {items_count} items:\n' + '\n'.join(item_details)

                        # Get most common GST rate from product instances
                        gst_rates = [p.product_gst for p in product_instances if p.product_gst]
                        most_common_gst = normalize_product_gst(max(set(gst_rates), key=gst_rates.count) if gst_rates else "18%")

                        # Create consolidated product
                        consolidated_product = TallyVendorConsolidatedProduct.objects.create(
                            vendor_bill_analyzed=analyzed_bill,
                            organization=organization,
                            item_name=f"Consolidated Items - {invoice_number} ({items_count} items)",
                            item_details=consolidated_details,
                            price=total_rounded,  # Total as rate
                            quantity=1,  # Always 1 for consolidated
                            amount=total_rounded,
                            product_gst=most_common_gst,  # 🎯 Most common GST rate from items
                            igst=igst_rounded,
                            cgst=cgst_rounded,
                            sgst=sgst_rounded,
                            original_items_count=items_count,
                            consolidation_notes=f'Auto-created during analysis for {items_count} items'
                        )

                        logger.info(f"✅ Auto-created consolidated product for bill {analyzed_bill.id} with {items_count} items (₹{total_amount})")

                    except Exception as e:
                        logger.error(f"❌ Error creating consolidated product for bill {analyzed_bill.id}: {str(e)}")
                        # Don't raise - consolidated product creation failure shouldn't break the main flow
                else:
                    logger.info(f"ℹ️ Skipping consolidated product creation - bill has only {len(product_instances)} item(s)")

            # Auto-fill round_off right after analysis so the UI shows a
            # balanced bill from the moment it opens (was blank until
            # user hit Verify, but Verify required the round-off ledger
            # picked first → chicken-and-egg). ``compute_round_off``
            # applies only when |diff| < ₹1 so genuine mismatches still
            # surface for review.
            try:
                analyzed_bill.compute_round_off()
            except Exception as round_off_err:
                logger.warning(
                    f"Auto round-off failed for vendor bill {analyzed_bill.id}: {round_off_err}"
                )

            # Update bill status
            bill.status = TallyVendorBill.BillStatus.ANALYSED
            bill.process = True
            bill.save(update_fields=['status', 'process'])

            logger.warning(f"🎉 ANALYSIS COMPLETE - Bill {bill.id} status updated to ANALYSED")

            return analyzed_bill

    except Exception as e:
        logger.error(f"💥 ERROR in process_analysis_data: {str(e)} - Data: {json_data}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise Exception(f"Error processing analysis data: {str(e)}")


def extract_items_from_properties(json_data):
    """Safely extract items from properties format"""
    try:
        items_data = safe_get_nested(json_data, ["properties", "items", "items"], [])
        if isinstance(items_data, list):
            extracted_items = []
            for item in items_data:
                if isinstance(item, dict):
                    extracted_item = {
                        "description": safe_get_nested(item, ["description", "const"], ""),
                        "quantity": safe_get_nested(item, ["quantity", "const"], 0),
                        "price": safe_get_nested(item, ["price", "const"], 0)
                    }
                    extracted_items.append(extracted_item)
            return extracted_items
        return []
    except Exception:
        return []


def find_appropriate_tax_ledger(organization, igst_val, cgst_val, sgst_val, ledger_type='bill'):
    """Delegates to shared find_tally_tax_ledger in helpers."""
    from .helpers import find_tally_tax_ledger
    return find_tally_tax_ledger(organization, igst_val, cgst_val, sgst_val, ledger_type=ledger_type)


def find_vendor_ledger(company_name, organization, vendor_gst=None):
    """Delegates to shared find_tally_vendor_ledger in helpers."""
    from .helpers import find_tally_vendor_ledger
    return find_tally_vendor_ledger(
        company_name, organization, vendor_gst=vendor_gst,
        similarity_threshold=0.70, gst_search_scope='parents'
    )


def process_pdf_splitting(pdf_file, organization, file_type, uploaded_by):
    """Split PDF into individual pages and create separate bills"""
    return split_pdf_to_bills(
        pdf_file=pdf_file,
        organization=organization,
        file_type=file_type,
        uploaded_by=uploaded_by,
        bill_model=TallyVendorBill,
        filename_prefix="BM-Page"
    )


# ============================================================================
# API Views
# ✅
@extend_schema(
    summary="List Vendor Bills",
    description="Get all vendor bills for the organization",
    responses={200: TallyVendorBillSerializer(many=True)},
    tags=['Tally Vendor Bills']
)
@api_view(['GET'])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def vendor_bills_list(request, org_id):
    """Get all vendor bills for the organization."""
    from .bill_helpers import bills_list_base
    return bills_list_base(
        request, org_id,
        bill_model=TallyVendorBill,
        serializer_class=TallyVendorBillSerializer,
        include_ownership_filter=True,
    )


# ✅
@extend_schema(
    summary="Upload Vendor Bills",
    description="Upload single or multiple vendor bill files (PDF, JPG, PNG). Supports both single file and multiple file uploads.",
    request=VendorBillUploadSerializer,
    responses={201: TallyVendorBillSerializer(many=True)},
    tags=['Tally Vendor Bills']
)
@api_view(['POST'])
@permission_classes([IsAuthenticated, IsOrgAdmin])
@parser_classes([MultiPartParser, FormParser])
def vendor_bills_upload(request, org_id):
    """Handle single or multiple vendor bill file uploads with PDF splitting support"""
    from .bill_helpers import bills_upload_base
    from ..tasks import enqueue_vendor_bill_processing, split_pdf_bill_vendor

    return bills_upload_base(
        request=request,
        org_id=org_id,
        bill_model=TallyVendorBill,
        upload_serializer_class=VendorBillUploadSerializer,
        response_serializer_class=TallyVendorBillSerializer,
        pdf_split_func=process_pdf_splitting,
        enqueue_func=enqueue_vendor_bill_processing,
        bill_type_label="vendor",
        pdf_split_task_fn=split_pdf_bill_vendor,
    )


# ✅
@extend_schema(
    summary="Check Bill Processing Status",
    description="Check the status of background processing for a vendor bill",
    responses={200: "Bill processing status information"},
    tags=['Tally Vendor Bills']
)
@api_view(['GET'])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def vendor_bill_processing_status(request, org_id, bill_id):
    """Check the processing status of a vendor bill"""
    from .bill_helpers import bill_processing_status_base
    return bill_processing_status_base(request, org_id, bill_id, TallyVendorBill, "Vendor")


# ✅
@extend_schema(
    summary="Analyze Vendor Bill",
    description="Analyze vendor bill using OpenAI to extract invoice data",
    request=BillAnalysisRequestSerializer,
    responses={
        200: TallyVendorAnalyzedBillSerializer,
        400: OpenApiResponse(description="Analysis failed")
    },
    tags=['Tally Vendor Bills']
)
@api_view(['POST'])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def vendor_bill_analyze(request, org_id):
    """Analyze vendor bill using OpenAI"""
    serializer = BillAnalysisRequestSerializer(data=request.data)
    if not serializer.is_valid():
        return Response({
            'error': 'Invalid Analysis Request',
            'message': 'The analysis request data is invalid. Please ensure the bill_id is provided and valid.',
            'details': serializer.errors,
            'error_code': 'INVALID_ANALYSIS_REQUEST'
        }, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

    bill_id = serializer.validated_data['bill_id']
    organization = get_organization_from_request(request, org_id)

    try:
        bill = TallyVendorBill.objects.alive().get(
            id=bill_id,
            organization=organization
        )
    except TallyVendorBill.DoesNotExist:
        return Response(
            {'error': 'Bill not found'},
            status=status.HTTP_404_NOT_FOUND
        )

    if bill.process:
        # Re-validate ownership using stored analysed_data so that bills processed
        # before the check_field fix get their description/flag corrected automatically.
        if bill.analysed_data:
            try:
                bill_belongs_to_org, ownership_description = validate_bill_ownership(bill.analysed_data, organization)
                if (bill.bill_belong_your_org != bill_belongs_to_org or
                        bill.description != ownership_description):
                    bill.bill_belong_your_org = bill_belongs_to_org
                    bill.description = ownership_description
                    bill.save(update_fields=['bill_belong_your_org', 'description'])
                    logger.info(
                        "Re-validated ownership for already-processed bill %s: %s",
                        bill.id, bill_belongs_to_org,
                    )
            except Exception as exc:
                logger.warning("Ownership re-validation failed for bill %s: %s", bill.id, exc)
        return Response({
            'message': 'Bill Already Analyzed',
            'data': 'This bill has already been processed and analyzed. Use the verification endpoint to modify the analyzed data.',
            'error_code': 'BILL_ALREADY_PROCESSED'
        }, status=status.HTTP_200_OK)

    try:
        # Check if bill already has analyzed data
        logger.warning(f"🔍 Checking if bill {bill_id} already has analyzed data...")
        
        if bill.analysed_data:
            logger.warning(f"📊 Bill has existing analyzed data, using process_existing_analysis_data()")
            analyzed_bill = process_existing_analysis_data(bill, bill.analysed_data, organization)
            logger.warning(f"✅ process_existing_analysis_data completed successfully")
        else:
            logger.warning(f"🚀 No existing data found, running new OpenAI analysis for bill {bill_id}")
            analysis_result = analyze_bill_with_ai(bill, organization)
            
            logger.warning(f"📋 analyze_bill_with_ai result: success={analysis_result.get('success', 'unknown')}")
            
            if not analysis_result.get('success'):
                logger.error(f"❌ Analysis failed: {analysis_result.get('error', 'Unknown error')}")
                return Response({
                    'error': 'Bill Analysis Failed',
                    'message': 'The bill analysis could not be completed. This might be due to poor image quality, unsupported file format, or AI service issues.',
                    'details': analysis_result.get('error', 'Unknown error'),
                    'error_code': 'ANALYSIS_FAILED'
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            
            analyzed_bill = analysis_result.get('analyzed_bill')
            logger.warning(f"✅ Got analyzed_bill from analysis_result: {analyzed_bill}")

        logger.warning(f"🔎 Starting duplicate check for bill {bill.bill_munshi_name}...")
        # Check for duplicate bills after analysis
        is_duplicate, duplicate_bills, max_similarity = check_duplicate_tally_vendor_bill(bill, organization)
        logger.warning(f"🔍 Duplicate check complete - is_duplicate: {is_duplicate}, count: {len(duplicate_bills) if duplicate_bills else 0}")

        # Persist duplicate metadata on the bill row so the list page's
        # duplicate badge + detail modal have data to render. Was
        # previously imported but never called — badge stayed empty.
        try:
            update_bill_duplicate_metadata(
                bill, duplicate_bills or [], max_similarity or 0,
            )
        except Exception as meta_err:
            logger.warning(
                "Failed to persist duplicate metadata for bill %s: %s",
                bill.id, meta_err,
            )

        response_data = {
            "detail": "Tally vendor bill analyzed successfully",
            "analyzed_bill": TallyVendorAnalyzedBillSerializer(analyzed_bill).data
        }
        
        logger.warning(f"📝 Created response_data with analyzed_bill serialized")

        # Add duplicate warnings if found
        if is_duplicate:
            logger.warning(f"⚠️  Processing duplicate warnings for {len(duplicate_bills)} duplicates")
            duplicate_warnings = []
            for dup in duplicate_bills:
                duplicate_warnings.append({
                    "duplicate_bill_id": str(dup['bill'].id),
                    "duplicate_bill_name": dup['bill'].bill_munshi_name,
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
                "warning_message": f"⚠️ DUPLICATE DETECTED: Found {len(duplicate_bills)} similar Tally vendor bill(s) in your organization. "
                                  f"This bill appears to be {round(max_similarity, 1)}% similar to existing bills. "
                                  "Please review carefully before proceeding to avoid duplicate entries."
            })

            logger.warning(f"Duplicate Tally vendor bill detected for {bill.bill_munshi_name} - {len(duplicate_bills)} similar bills found")

        logger.warning(f"🎉 API Response ready - sending success response with status 200")
        return Response(response_data, status=status.HTTP_200_OK)

    except Exception as e:
        logger.error(f"💥 CRITICAL ERROR in vendor_bill_analyze: {str(e)}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return Response({
            'error': 'Bill Analysis Failed',
            'message': 'The bill analysis could not be completed. This might be due to poor image quality, unsupported file format, or AI service issues.',
            'details': str(e),
            'error_code': 'ANALYSIS_FAILED'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


def process_existing_analysis_data(bill, existing_data, organization):
    """Process existing analyzed data without calling OpenAI again"""
    try:
        logger.info(f"Processing existing analysis data for bill {bill.id}")
        
        # Validate bill ownership with existing data
        bill_belongs_to_org, ownership_description = validate_bill_ownership(existing_data, organization)
        
        # Update bill ownership fields
        bill.bill_belong_your_org = bill_belongs_to_org
        bill.description = ownership_description
        bill.save(update_fields=['bill_belong_your_org', 'description'])
        
        logger.info(f"Updated ownership for existing bill: {bill_belongs_to_org}, Description: {ownership_description}")
        logger.info(f"Processing existing analyzed data for bill {bill.id}")

        # Check if analyzed bill already exists
        try:
            analyzed_bill = TallyVendorAnalyzedBill.objects.get(selected_bill=bill)
            logger.info(f"Found existing analyzed bill {analyzed_bill.id}")
            return analyzed_bill
        except TallyVendorAnalyzedBill.DoesNotExist:
            pass

        # Extract required fields with safe access
        invoice_number = str(existing_data.get('invoiceNumber', '')).strip()
        date_issued = str(existing_data.get('dateIssued', ''))

        # Handle 'from' field safely
        from_data = existing_data.get('from', {})
        if isinstance(from_data, dict):
            company_name = str(from_data.get('name', '')).strip().lower()
        else:
            company_name = str(from_data).strip().lower()

        # Parse date with multiple format support
        bill_date = parse_bill_date(date_issued)

        # Find vendor ledger
        vendor = find_vendor_ledger(company_name, organization)

        # Determine GST type with safe conversion and proper decimal rounding
        igst_val = round(safe_float_convert(existing_data.get('igst', 0)), 2)
        cgst_val = round(safe_float_convert(existing_data.get('cgst', 0)), 2)
        sgst_val = round(safe_float_convert(existing_data.get('sgst', 0)), 2)
        total_val = round(safe_float_convert(existing_data.get('total', 0)), 2)

        if igst_val > 0:
            gst_type = TallyVendorAnalyzedBill.GSTType.IGST
        elif cgst_val > 0 or sgst_val > 0:
            gst_type = TallyVendorAnalyzedBill.GSTType.CGST_SGST
        else:
            gst_type = TallyVendorAnalyzedBill.GSTType.UNKNOWN

        # 🏛️ FIND APPROPRIATE TAX LEDGERS FOR BILL-LEVEL GST VALUES
        logger.warning(f"🏛️ [EXISTING BILL] Finding tax ledgers - IGST: {igst_val}, CGST: {cgst_val}, SGST: {sgst_val}")
        
        igst_tax_ledger = None
        cgst_tax_ledger = None
        sgst_tax_ledger = None
        
        if igst_val > 0:
            igst_tax_ledger = find_appropriate_tax_ledger(organization, igst_val, 0, 0, ledger_type='bill')
            logger.warning(f"🎯 [EXISTING BILL] IGST Tax Ledger: {igst_tax_ledger.name if igst_tax_ledger else 'None'}")
        
        if cgst_val > 0:
            cgst_tax_ledger = find_appropriate_tax_ledger(organization, 0, cgst_val, 0, ledger_type='bill')
            logger.warning(f"🎯 [EXISTING BILL] CGST Tax Ledger: {cgst_tax_ledger.name if cgst_tax_ledger else 'None'}")
        
        if sgst_val > 0:
            sgst_tax_ledger = find_appropriate_tax_ledger(organization, 0, 0, sgst_val, ledger_type='bill')
            logger.warning(f"🎯 [EXISTING BILL] SGST Tax Ledger: {sgst_tax_ledger.name if sgst_tax_ledger else 'None'}")

        # Create analyzed bill without Django validation to avoid GST mismatch errors
        with transaction.atomic():
            # Create the analyzed bill instance without calling save() initially
            analyzed_bill = TallyVendorAnalyzedBill(
                selected_bill=bill,
                vendor=vendor,
                bill_no=invoice_number,
                bill_date=bill_date,
                igst=igst_val,
                cgst=cgst_val,
                sgst=sgst_val,
                igst_taxes=igst_tax_ledger,  # 🎯 Assign IGST tax ledger
                cgst_taxes=cgst_tax_ledger,  # 🎯 Assign CGST tax ledger
                sgst_taxes=sgst_tax_ledger,  # 🎯 Assign SGST tax ledger
                discount=0,  # Default discount value for existing data
                total=total_val,
                note="AI Analyzed Bill (Existing Data)",
                organization=organization,
                gst_type=gst_type
            )

            # Save without calling clean() to skip validation
            analyzed_bill.save(skip_validation=True)
            logger.warning(f"✅ [EXISTING BILL] Saved analyzed bill with tax ledgers - IGST: {analyzed_bill.igst_taxes}, CGST: {analyzed_bill.cgst_taxes}, SGST: {analyzed_bill.sgst_taxes}")

            # Create analyzed products with proper GST calculation
            product_instances = []
            items = existing_data.get('items', [])
            total_products_amount = 0  # Track total amount for GST distribution

            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        price = round(safe_float_convert(item.get('price', 0)), 2)
                        quantity = safe_int_convert(item.get('quantity', 0))
                        amount = round(price * quantity, 2)
                        total_products_amount += amount

                        product_instances.append({
                            'item': item,
                            'price': price,
                            'quantity': quantity,
                            'amount': amount
                        })

            # Now calculate GST distribution across products
            created_products = []
            for product_data in product_instances:
                item = product_data['item']
                price = product_data['price']
                quantity = product_data['quantity']
                amount = product_data['amount']

                # Calculate proportional GST for this product
                if total_products_amount > 0:
                    proportion = amount / total_products_amount
                    product_igst = round(igst_val * proportion, 2)
                    product_cgst = round(cgst_val * proportion, 2)
                    product_sgst = round(sgst_val * proportion, 2)

                    # Calculate GST rate based on the allocated GST amount
                    if amount > 0:
                        if gst_type == TallyVendorAnalyzedBill.GSTType.IGST:
                            gst_rate = round((product_igst / amount) * 100, 2)
                        elif gst_type == TallyVendorAnalyzedBill.GSTType.CGST_SGST:
                            total_product_gst = product_cgst + product_sgst
                            gst_rate = round((total_product_gst / amount) * 100, 2)
                        else:
                            gst_rate = 0
                    else:
                        gst_rate = 0
                        product_igst = 0
                        product_cgst = 0
                        product_sgst = 0
                else:
                    gst_rate = 0
                    product_igst = 0
                    product_cgst = 0
                    product_sgst = 0

                # � AUTO-ASSIGN LINE ITEM LEDGER FROM CHART OF ACCOUNTS (EXISTING DATA)
                logger.warning(f"🏛️  [EXISTING] Finding line item ledger for product '{item.get('description', 'No desc')[:30]}...'")
                item_ledger = find_appropriate_tax_ledger(organization, product_igst, product_cgst, product_sgst, ledger_type='product')
                
                # Create product instance without validation
                product = TallyVendorAnalyzedProduct(
                    vendor_bill_analyzed=analyzed_bill,
                    item_details=str(item.get('description', '')),
                    price=price,
                    quantity=quantity,
                    amount=amount,
                    taxes=item_ledger,  # 🎯 Line item ledger from chart_of_accounts_parents
                    product_gst=normalize_product_gst(gst_rate),  # 🎯 Properly formatted GST rate
                    igst=product_igst,
                    cgst=product_cgst,
                    sgst=product_sgst,
                    organization=organization
                )
                
                logger.warning(f"📦 [EXISTING] Created product: '{item.get('description', 'No desc')[:30]}...' | Line Item Ledger: '{item_ledger.name if item_ledger else 'None'}' (Parent: {item_ledger.parent.parent if item_ledger else 'N/A'}) | GST: {gst_rate}%")
                created_products.append(product)

            # Bulk create products without validation
            if created_products:
                TallyVendorAnalyzedProduct.objects.bulk_create(created_products)
                logger.info(f"Successfully created {len(created_products)} products for bill {analyzed_bill.id}")

                # ✅ AUTO-CREATE CONSOLIDATED PRODUCT FOR MULTI-ITEM BILLS
                if len(created_products) > 1:
                    try:
                        # Delete existing consolidated product if exists
                        TallyVendorConsolidatedProduct.objects.filter(vendor_bill_analyzed=analyzed_bill).delete()

                        # Calculate consolidated data
                        total_amount = sum(p.amount for p in created_products)
                        items_count = len(created_products)

                        # Create detailed breakdown
                        item_details = []
                        for product in created_products:
                            item_details.append(f'• {product.item_details} (Qty: {product.quantity}, Rate: ₹{product.price})')

                        consolidated_details = f'Consolidated {items_count} items:\n' + '\n'.join(item_details)

                        # Get most common GST rate from created products
                        gst_rates = [p.product_gst for p in created_products if p.product_gst]
                        most_common_gst = normalize_product_gst(max(set(gst_rates), key=gst_rates.count) if gst_rates else "18%")

                        # Create consolidated product
                        consolidated_product = TallyVendorConsolidatedProduct.objects.create(
                            vendor_bill_analyzed=analyzed_bill,
                            organization=organization,
                            item_name=f"Consolidated Items - {invoice_number} ({items_count} items)",
                            item_details=consolidated_details,
                            price=total_amount,  # Total as rate
                            quantity=1,  # Always 1 for consolidated
                            amount=total_amount,
                            product_gst=most_common_gst,  # 🎯 Normalized GST rate
                            igst=igst_val,
                            cgst=cgst_val,
                            sgst=sgst_val,
                            original_items_count=items_count,
                            consolidation_notes=f'Auto-created during analysis for {items_count} items'
                        )

                        logger.info(f"✅ Auto-created consolidated product for bill {analyzed_bill.id} with {items_count} items (₹{total_amount})")

                    except Exception as e:
                        logger.error(f"❌ Error creating consolidated product for bill {analyzed_bill.id}: {str(e)}")
                        # Don't raise - consolidated product creation failure shouldn't break the main flow
                else:
                    logger.info(f"ℹ️ Skipping consolidated product creation - bill has only {len(created_products)} item(s)")

            # Update bill status
            bill.status = TallyVendorBill.BillStatus.ANALYSED
            bill.process = True
            bill.save(update_fields=['status', 'process'])

            logger.info(f"Successfully processed existing analysis data for bill {bill.id}")
            return analyzed_bill

    except Exception as e:
        logger.error(f"Error processing existing analysis data: {str(e)}")
        raise Exception(f"Error processing existing analysis data: {str(e)}")


# ============================================================================
# Get Vendor Bill Detail
# ✅
@extend_schema(
    summary="Get Vendor Bill Details",
    description="Get vendor bill detail including analysis data and next bill to process",
    responses={200: "TallyVendorBillDetailSerializer"},
    tags=['Tally Vendor Bills']
)
@api_view(['GET'])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def vendor_bill_detail(request, org_id, bill_id):
    """Get vendor bill detail including analysis data using proper serializer"""
    from ..serializers import TallyVendorBillDetailSerializer
    from .bill_helpers import bill_detail_base
    return bill_detail_base(request, org_id, bill_id, TallyVendorBill, TallyVendorBillDetailSerializer)


# ===========================================================================
# Bill Verify View
from decimal import Decimal, InvalidOperation
from apps.common.converters import to_decimal as _to_decimal, to_int as _to_int  # noqa: E402


def calculate_product_gst(amount, product_gst_str, gst_type):
    """
    Calculate IGST, CGST, and SGST based on amount, product_gst percentage, and gst_type.

    Args:
        amount: Product amount (Decimal or float)
        product_gst_str: GST percentage string (e.g., "18%", "12%")
        gst_type: GST type from TallyVendorAnalyzedBill.GSTType

    Returns:
        tuple: (igst, cgst, sgst) as Decimal values
    """
    try:
        # Convert amount to Decimal
        amount_decimal = _to_decimal(amount, "0")

        # Extract GST percentage from string (e.g., "18%" -> 18.0)
        if not product_gst_str or product_gst_str == "":
            return Decimal("0"), Decimal("0"), Decimal("0")

        gst_percentage_str = str(product_gst_str).strip().replace('%', '')
        if not gst_percentage_str:
            return Decimal("0"), Decimal("0"), Decimal("0")

        gst_percentage = Decimal(gst_percentage_str)

        # Calculate total GST amount
        gst_amount = (amount_decimal * gst_percentage) / Decimal("100")

        # Distribute GST based on gst_type
        if gst_type == TallyVendorAnalyzedBill.GSTType.IGST:
            # All GST goes to IGST
            return round(gst_amount, 2), Decimal("0"), Decimal("0")
        elif gst_type == TallyVendorAnalyzedBill.GSTType.CGST_SGST:
            # Split equally between CGST and SGST
            half_gst = gst_amount / Decimal("2")
            return Decimal("0"), round(half_gst, 2), round(half_gst, 2)
        else:
            # Unknown GST type - return zeros
            return Decimal("0"), Decimal("0"), Decimal("0")

    except (ValueError, InvalidOperation) as e:
        logger.warning(f"Error calculating product GST: {str(e)}")
        return Decimal("0"), Decimal("0"), Decimal("0")


# ✅
@extend_schema(
    summary="Verify Vendor Bill",
    description="Verify analyzed vendor bill data and mark as verified",
    request=BillVerificationSerializer,
    responses={200: TallyVendorAnalyzedBillSerializer},
    tags=['Tally Vendor Bills']
)
@api_view(['POST'])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def vendor_bill_verify(request, org_id):
    bill_id = request.data.get('bill_id')
    analyzed_bill_id = request.data.get('analyzed_bill')  # OPTIONAL in your payload
    analyzed_data = request.data.get('analyzed_data')

    organization = get_organization_from_request(request, org_id)
    if not organization:
        return Response({
            'error': 'Organization Access Denied',
            'message': f'Organization with ID {org_id} not found or you do not have access to it. Please check the organization ID and your permissions.',
            'error_code': 'ORG_NOT_FOUND'
        }, status=status.HTTP_404_NOT_FOUND)

    if not bill_id:
        return Response({
            'error': 'Missing Bill ID',
            'message': 'The bill_id parameter is required for verification. Please provide a valid bill ID.',
            'error_code': 'BILL_ID_REQUIRED'
        }, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

    try:
        bill = TallyVendorBill.objects.alive().get(id=bill_id, organization=organization)

        # Handle potential multiple analyzed bills - use the most recent one
        try:
            analyzed_bill = TallyVendorAnalyzedBill.objects.get(selected_bill=bill, organization=organization)
        except TallyVendorAnalyzedBill.MultipleObjectsReturned:
            # Multiple analyzed bills exist - use the most recent one and log warning
            logger.warning(f"Multiple TallyVendorAnalyzedBill found for bill {bill_id}, using the most recent one")
            analyzed_bill = TallyVendorAnalyzedBill.objects.filter(
                selected_bill=bill,
                organization=organization
            ).order_by('-created_at').first()

            # Optionally clean up duplicate records (keep only the most recent)
            duplicate_bills = TallyVendorAnalyzedBill.objects.filter(
                selected_bill=bill,
                organization=organization
            ).exclude(id=analyzed_bill.id)

            if duplicate_bills.exists():
                duplicate_count = duplicate_bills.count()
                duplicate_bills.delete()
                logger.info(f"Cleaned up {duplicate_count} duplicate TallyVendorAnalyzedBill records for bill {bill_id}")

    except (TallyVendorBill.DoesNotExist, TallyVendorAnalyzedBill.DoesNotExist):
        return Response({
            'error': 'Bill or Analysis Data Not Found',
            'message': f'Bill with ID {bill_id} or its analyzed data not found. Please ensure the bill exists and has been analyzed.',
            'error_code': 'BILL_OR_ANALYSIS_NOT_FOUND'
        }, status=status.HTTP_404_NOT_FOUND)

    # Optional: ensure client-sent analyzed_bill matches what we resolved from bill
    if analyzed_bill_id and str(analyzed_bill.id) != str(analyzed_bill_id):
        return Response({
            'error': 'Mismatched Analysis Data',
            'message': 'The provided analyzed_bill ID does not match the bill_id. Please ensure the correct analyzed bill data is being referenced.',
            'error_code': 'ANALYSIS_MISMATCH'
        }, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

    # Allow re-verification of Synced bills only if tally_synced is still False
    allowed_statuses = [TallyVendorBill.BillStatus.ANALYSED, TallyVendorBill.BillStatus.VERIFIED]
    if bill.status == TallyVendorBill.BillStatus.SYNCED and not bill.tally_synced:
        allowed_statuses.append(TallyVendorBill.BillStatus.SYNCED)

    if bill.status not in allowed_statuses:
        return Response({
            'error': 'Invalid Bill Status',
            'message': f'Bill must be in "Analysed", "Verified", or "Synced" (not yet posted to Tally) status to perform verification. Current status: {bill.status}',
            'current_status': bill.status,
            'required_status': ['Analysed', 'Verified', 'Synced (not posted)'],
            'error_code': 'INVALID_BILL_STATUS'
        }, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

    try:
        # Handle consolidation flag from root level or analyzed_data
        consolidate_flag = request.data.get('consolidate', analyzed_data.get('consolidate', False))
        logger.info(f"Received consolidate flag: {consolidate_flag}")
        logger.info(f"Current analyzed_bill consolidate value: {getattr(analyzed_bill, 'consolidate', 'NOT SET')}")

        # Always update the consolidate flag regardless of current value
        try:
            analyzed_bill.consolidate = consolidate_flag
            analyzed_bill.save(update_fields=['consolidate'])
            logger.info(f"Successfully updated consolidate flag to: {consolidate_flag}")
        except Exception as consolidate_error:
            logger.error(f"Failed to update consolidate flag: {str(consolidate_error)}")
            # If the field doesn't exist, try to save without it but log the issue
            logger.warning("Consolidate field might not exist in the model")

        verified_bill = update_analyzed_bill_data(analyzed_bill, analyzed_data, organization)

        # Tax reconciliation: Σ(per-line tax) vs bill-level tax. Used to be a
        # hard 422 block, but the UI now keeps line totals and bill-level
        # totals in lock-step (the bill-level fields are derived from the
        # line items), so any residual gap is almost always sub-paisa
        # floating-point noise. Per product feedback the OCR/line mismatch
        # must be informational only — the user should always be able to
        # verify. Downgraded to a logged warning here.
        TOL = TallyVendorAnalyzedBill.ROUND_OFF_THRESHOLD  # Decimal('1.00')

        def _to_decimal(value):
            if value is None:
                return Decimal('0')
            if isinstance(value, Decimal):
                return value
            try:
                return Decimal(str(value))
            except (InvalidOperation, ValueError, TypeError):
                return Decimal('0')

        product_qs = list(verified_bill.products.all())
        line_cgst = sum((_to_decimal(p.cgst) for p in product_qs), Decimal('0'))
        line_sgst = sum((_to_decimal(p.sgst) for p in product_qs), Decimal('0'))
        line_igst = sum((_to_decimal(p.igst) for p in product_qs), Decimal('0'))
        bill_cgst = _to_decimal(verified_bill.cgst)
        bill_sgst = _to_decimal(verified_bill.sgst)
        bill_igst = _to_decimal(verified_bill.igst)
        diffs = {
            'cgst': abs(line_cgst - bill_cgst),
            'sgst': abs(line_sgst - bill_sgst),
            'igst': abs(line_igst - bill_igst),
        }
        breaches = {k: float(v) for k, v in diffs.items() if v >= TOL}
        if breaches:
            logger.warning(
                "Tax reconciliation drift on bill %s (>= ₹%s) — verifying anyway. "
                "diffs=%s line_totals={cgst:%s, sgst:%s, igst:%s} "
                "bill_totals={cgst:%s, sgst:%s, igst:%s}",
                verified_bill.id, TOL, breaches,
                line_cgst, line_sgst, line_igst,
                bill_cgst, bill_sgst, bill_igst,
            )

        # Recompute round-off after products & tax fields are persisted so the
        # XML sync payload can carry an accurate Round Off entry.
        try:
            verified_bill.compute_round_off()
        except Exception as round_off_err:
            logger.warning(
                f"Round-off computation failed for bill {verified_bill.id}: {round_off_err}"
            )

        bill.status = TallyVendorBill.BillStatus.VERIFIED
        bill.save(update_fields=['status'])

        response_data = get_structured_bill_data(verified_bill, organization)
        return Response({
            "message": "Bill verified successfully",
            "analyzed_data": response_data
        }, status=status.HTTP_200_OK)

    except Exception as e:
        logger.error(f"Bill verification failed: {str(e)}")
        return Response({
            'error': 'Bill Verification Failed',
            'message': 'The bill verification process encountered an error. This could be due to invalid data or system issues. Please check your entries and try again.',
            'details': str(e),
            'error_code': 'VERIFICATION_FAILED'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


def update_analyzed_bill_data(analyzed_bill, analyzed_data, organization):
    """Update analyzed bill with user modifications"""

    if not analyzed_data:
        return analyzed_bill

    # Add defensive check to ensure analyzed_bill is the correct type
    from ..models import TallyVendorAnalyzedBill
    if not isinstance(analyzed_bill, TallyVendorAnalyzedBill):
        logger.error(f"Expected TallyVendorAnalyzedBill, got {type(analyzed_bill)}")
        raise ValueError(f"Invalid analyzed_bill type: {type(analyzed_bill)}")

    # Coerce JSON numbers (may arrive as int/float/str) to Decimal at the model
    # boundary. Storing floats on Decimal model fields and then reading them
    # back without `refresh_from_db()` returns the raw float — which then
    # blows up any Decimal arithmetic later in the request (e.g. the verify
    # reconciliation block).
    _Q2 = Decimal('0.01')

    def _money(value):
        if value is None or value == '':
            return Decimal('0.00')
        try:
            return Decimal(str(value)).quantize(_Q2)
        except (InvalidOperation, ValueError, TypeError):
            return Decimal('0.00')

    from ..models import Ledger as _Ledger

    def _resolve_tax_ledger(payload, tax_type):
        """Return a Ledger — prefer ``ledger_id`` UUID over ``ledger`` name.
        Name-only lookups can silently pick a duplicate ledger from a
        different parent group; UUID from the frontend dropdown is
        authoritative.
        """
        if not isinstance(payload, dict):
            return None
        ledger_id = payload.get('ledger_id') or payload.get('id')
        if ledger_id:
            try:
                return _Ledger.objects.get(id=ledger_id, organization=organization)
            except (_Ledger.DoesNotExist, ValueError):
                logger.warning(
                    "Verify: ledger_id %s not in org %s — falling back to name",
                    ledger_id, organization.id,
                )
        name = (payload.get('ledger') or "").strip()
        # Case-insensitive sentinel guard — matches ``_BLANK_LEDGER_SENTINELS``
        # used in the product tax-ledger path below. Prevents accidental
        # creation of a bogus ledger literally named "no tax ledger".
        if name and name.lower() not in {
            "", "no tax ledger", "no purchase ledger", "none", "null",
        }:
            return find_or_create_tax_ledger(name, tax_type, organization)
        return None

    with transaction.atomic():
        # Handle consolidate flag if present in analyzed_data
        if 'consolidate' in analyzed_data:
            consolidate_value = analyzed_data['consolidate']
            logger.info(f"Updating consolidate flag in analyzed_data to: {consolidate_value}")
            try:
                analyzed_bill.consolidate = consolidate_value
                logger.info(f"Set analyzed_bill.consolidate to: {consolidate_value}")
            except AttributeError:
                logger.warning("Consolidate field not available in analyzed_bill model")

        # Update vendor information. Prefer explicit ``vendor_id`` UUID
        # sent by the frontend dropdown — name-based fuzzy match can
        # silently pick a wrong vendor when two ledgers share a name
        # across parent groups. Falls back to name lookup only when
        # the frontend didn't send an ID (legacy shape).
        vendor_data = analyzed_data.get('vendor', {})
        if vendor_data:
            vendor_id = vendor_data.get('vendor_id') or vendor_data.get('id')
            if vendor_id:
                try:
                    from ..models import Ledger as _Ledger
                    analyzed_bill.vendor = _Ledger.objects.get(
                        id=vendor_id, organization=organization,
                    )
                except (_Ledger.DoesNotExist, ValueError):
                    logger.warning(
                        "Verify: vendor_id %s not found in org %s — "
                        "falling back to name match",
                        vendor_id, organization.id,
                    )
                    vendor_id = None
            if not vendor_id and vendor_data.get('vendor_name') != "No Ledger":
                vendor_name = vendor_data.get('vendor_name')
                if vendor_name:
                    current_vendor = analyzed_bill.vendor
                    if not current_vendor or current_vendor.name != vendor_name.strip():
                        vendor = find_or_create_vendor_ledger(vendor_name, vendor_data, organization)
                        if vendor:
                            analyzed_bill.vendor = vendor
                    else:
                        if vendor_data.get('master_id') and vendor_data['master_id'] != "No Ledger":
                            current_vendor.master_id = vendor_data['master_id']
                        if vendor_data.get('gst_in') and vendor_data['gst_in'] != "No Ledger":
                            current_vendor.gst_in = vendor_data['gst_in']
                        if vendor_data.get('company') and vendor_data['company'] != "No Ledger":
                            current_vendor.company = vendor_data['company']
                        current_vendor.save()

        # Update bill details
        if 'bill_no' in analyzed_data:
            analyzed_bill.bill_no = analyzed_data['bill_no']
        if 'bill_date' in analyzed_data:
            # Parse date string (format: "08-03-2021")
            bill_date = parse_bill_date(analyzed_data['bill_date'])
            if bill_date:
                analyzed_bill.bill_date = bill_date
        if 'due_date' in analyzed_data:
            # Parse due date string (format: "08-03-2021")
            due_date = parse_bill_date(analyzed_data['due_date'])
            if due_date:
                analyzed_bill.due_date = due_date
        if 'total_amount' in analyzed_data:
            analyzed_bill.total = _money(analyzed_data['total_amount'])

        # Update tax information. All 7 fields (igst/cgst/sgst/discount/
        # cess/freight/round_off) share the same shape → single loop.
        # Ledger FK cleared when amount is zero so stale references
        # don't leak into the sync XML.
        taxes_data = analyzed_data.get('taxes', {})
        if taxes_data:
            _TAX_FIELDS = (
                ('igst',      'igst',      'igst_taxes',      'IGST'),
                ('cgst',      'cgst',      'cgst_taxes',      'CGST'),
                ('sgst',      'sgst',      'sgst_taxes',      'SGST'),
                ('discount',  'discount',  'discount_taxes',  'DISCOUNT'),
                ('cess',      'cess',      'cess_taxes',      'CESS'),
                ('freight',   'freight',   'freight_taxes',   'FREIGHT'),
                ('round_off', 'round_off', 'round_off_taxes', 'ROUND_OFF'),
            )
            for payload_key, amount_field, ledger_field, tax_type in _TAX_FIELDS:
                block = taxes_data.get(payload_key)
                if not isinstance(block, dict):
                    continue
                if 'amount' in block:
                    amt = _money(block['amount'])
                    setattr(analyzed_bill, amount_field, amt)
                    if amt == 0:
                        # Amount cleared — drop the FK so the sync XML
                        # doesn't emit a ghost ledger row.
                        setattr(analyzed_bill, ledger_field, None)
                        continue
                resolved = _resolve_tax_ledger(block, tax_type)
                if resolved:
                    setattr(analyzed_bill, ledger_field, resolved)

        # Determine GST type based on updated amounts
        if analyzed_bill.igst and analyzed_bill.igst > 0:
            analyzed_bill.gst_type = TallyVendorAnalyzedBill.GSTType.IGST
        elif (analyzed_bill.cgst and analyzed_bill.cgst > 0) or (analyzed_bill.sgst and analyzed_bill.sgst > 0):
            analyzed_bill.gst_type = TallyVendorAnalyzedBill.GSTType.CGST_SGST
        else:
            analyzed_bill.gst_type = TallyVendorAnalyzedBill.GSTType.UNKNOWN

        # Save the analyzed bill
        # Persist optional note field if provided
        if 'note' in analyzed_data:
            # Ensure note is a string and trim whitespace
            note_val = analyzed_data.get('note') or ''
            try:
                analyzed_bill.note = str(note_val).strip()
            except Exception:
                analyzed_bill.note = ''

        analyzed_bill.save(skip_validation=True)

        # Update line items (products) and handle consolidated products
        consolidate_flag = analyzed_data.get('consolidate', False)

        logger.info(f"Processing products with consolidate flag: {consolidate_flag}")

        if consolidate_flag:
            # Handle consolidation mode - use consolidate_prod array
            logger.info("Processing in consolidation mode - looking for consolidate_prod array")

            consolidate_prod_array = analyzed_data.get('consolidate_prod', [])
            if consolidate_prod_array:
                logger.info(f"Found {len(consolidate_prod_array)} items in consolidate_prod array")

                try:
                    # 🔄 FIRST: Clear existing consolidated products to prevent duplicates (like Zoho)
                    existing_consolidated = TallyVendorConsolidatedProduct.objects.filter(vendor_bill_analyzed=analyzed_bill)
                    if existing_consolidated.exists():
                        existing_count = existing_consolidated.count()
                        existing_consolidated.delete()
                        logger.info(f"Deleted {existing_count} existing consolidated products before creating new ones")

                    # Handle consolidated product creation (always create new after clearing)
                    for idx, consolidated_product_data in enumerate(consolidate_prod_array):
                        item_id = consolidated_product_data.get('item_id')
                        item_name = consolidated_product_data.get('item_name', 'Unnamed')
                        logger.info(f"Creating consolidated product {idx + 1}: {item_name} (item_id: {item_id}, type: {type(item_id)})")

                        try:
                            # Find or create tax ledger with better logging
                            tax_ledger = None
                            tax_ledger_name = consolidated_product_data.get('tax_ledger')
                            logger.info(f"Processing tax_ledger: '{tax_ledger_name}' for item {idx + 1}")

                            if tax_ledger_name and tax_ledger_name not in [None, '', 'No Tax Ledger']:
                                tax_ledger = find_or_create_tax_ledger(tax_ledger_name, 'purchase', organization)
                                if tax_ledger:
                                    logger.info(f"Found/created tax ledger: {tax_ledger.name} (ID: {tax_ledger.id})")
                                    # Validate that the tax ledger belongs to the same organization
                                    if tax_ledger.organization != organization:
                                        logger.error(f"Tax ledger organization mismatch! Ledger org: {tax_ledger.organization.id}, Expected org: {organization.id}")
                                        tax_ledger = None
                                else:
                                    logger.warning(f"Failed to find/create tax ledger: {tax_ledger_name}")
                            else:
                                logger.info(f"No tax ledger specified or invalid value: '{tax_ledger_name}'")

                            # Create new consolidated product (Tally now uses ForeignKey, multiple supported)
                            consolidated_product = TallyVendorConsolidatedProduct.objects.create(
                                vendor_bill_analyzed=analyzed_bill,
                                organization=organization,
                                item_name=consolidated_product_data.get('item_name') or f'Consolidated Item {idx + 1}',
                                item_details=consolidated_product_data.get('item_details') or '',
                                quantity=int(consolidated_product_data.get('quantity', 1)),
                                price=float(consolidated_product_data.get('price', 0) or consolidated_product_data.get('rate', 0)),
                                amount=float(consolidated_product_data.get('amount', 0)),
                                taxes=tax_ledger,  # This should save the tax ledger
                                product_gst=consolidated_product_data.get('product_gst') or '',
                                igst=float(consolidated_product_data.get('igst', 0)),
                                cgst=float(consolidated_product_data.get('cgst', 0)),
                                sgst=float(consolidated_product_data.get('sgst', 0)),
                                original_items_count=consolidated_product_data.get('original_items_count', 1),
                                consolidation_notes='Created from frontend verification'
                            )

                            # Log the saved tax ledger info
                            logger.info(f"Created consolidated product {consolidated_product.id} with:")
                            logger.info(f"  - Item: {consolidated_product.item_name}")
                            logger.info(f"  - Tax Ledger: {consolidated_product.taxes.name if consolidated_product.taxes else 'None'}")
                            logger.info(f"  - Tax Ledger ID: {consolidated_product.taxes.id if consolidated_product.taxes else 'None'}")

                            # Double-check by re-fetching from database to ensure tax ledger was saved
                            saved_product = TallyVendorConsolidatedProduct.objects.get(id=consolidated_product.id)
                            if saved_product.taxes:
                                logger.info(f"✅ VERIFIED: Tax ledger saved correctly - {saved_product.taxes.name}")
                            else:
                                logger.error(f"❌ ERROR: Tax ledger not saved to database for product {consolidated_product.id}")

                            # Since Tally now uses ForeignKey, multiple consolidated products are supported
                            # We can create each consolidated product from the array

                        except Exception as consolidate_item_error:
                            logger.error(f"Error creating consolidated product {idx + 1}: {consolidate_item_error}")
                            continue

                except Exception as consolidate_error:
                    logger.error(f"Error processing consolidate_prod array: {consolidate_error}")

                # Keep individual products intact since users might switch between layouts
                existing_individual_products = analyzed_bill.products.all()
                if existing_individual_products.exists() and len(consolidate_prod_array) > 0:
                    logger.info(f"Keeping {existing_individual_products.count()} individual products (layout switching support)")
                    # Individual products are preserved for layout flexibility
                elif len(consolidate_prod_array) == 0:
                    logger.warning("No consolidated products provided - keeping existing individual products")

            else:
                logger.warning("Consolidate=true but no consolidate_prod array found in payload")

                # Fallback: Check if consolidated product exists in regular products array
                line_items = analyzed_data.get('products', [])
                if line_items:
                    logger.info("Attempting to find consolidated product in products array as fallback")

                    consolidated_product_from_payload = None
                    for item in line_items:
                        # Check if this is a consolidated product (has consolidated item details)
                        if (item.get('item_details') and
                            'Consolidated' in str(item.get('item_details')) and
                            'items:' in str(item.get('item_details'))):
                            consolidated_product_from_payload = item
                            logger.info(f"Found consolidated product in products array: {item.get('item_name')}")
                            break

                    if consolidated_product_from_payload:
                        # Process consolidated product from products array fallback
                        try:
                            # Find or create tax ledger with logging
                            tax_ledger = None
                            tax_ledger_name = consolidated_product_from_payload.get('tax_ledger')
                            logger.info(f"Processing fallback tax_ledger: '{tax_ledger_name}'")

                            if tax_ledger_name and tax_ledger_name not in [None, '', "No Tax Ledger"]:
                                tax_ledger = find_or_create_tax_ledger(tax_ledger_name, 'purchase', organization)
                                if tax_ledger:
                                    logger.info(f"Found/created tax ledger for fallback: {tax_ledger.name} (ID: {tax_ledger.id})")
                                else:
                                    logger.warning(f"Failed to find/create tax ledger for fallback: {tax_ledger_name}")

                            # Create new consolidated product (since we allow multiple now)
                            consolidated_data = {
                                'vendor_bill_analyzed': analyzed_bill,
                                'organization': organization,
                                'item_name': consolidated_product_from_payload.get('item_name', 'Consolidated Items'),
                                'item_details': consolidated_product_from_payload.get('item_details', ''),
                                'quantity': int(consolidated_product_from_payload.get('quantity', 1)),
                                'price': float(consolidated_product_from_payload.get('price', 0) or consolidated_product_from_payload.get('rate', 0)),
                                'amount': float(consolidated_product_from_payload.get('amount', 0)),
                                'taxes': tax_ledger,
                                'product_gst': consolidated_product_from_payload.get('product_gst', ''),
                                'igst': float(consolidated_product_from_payload.get('igst', 0)),
                                'cgst': float(consolidated_product_from_payload.get('cgst', 0)),
                                'sgst': float(consolidated_product_from_payload.get('sgst', 0)),
                                'original_items_count': consolidated_product_from_payload.get('original_items_count', 1),
                                'consolidation_notes': "Updated via verification (fallback from products array)"
                            }

                            # Create new consolidated product (since we allow multiple now)
                            consolidated_product = TallyVendorConsolidatedProduct.objects.create(**consolidated_data)
                            logger.info(f"Created new consolidated product (fallback): {consolidated_product.id}")

                            # Keep individual products intact for layout switching support
                            existing_individual_products = analyzed_bill.products.all()
                            if existing_individual_products.exists() and consolidated_product:
                                logger.info(f"Keeping {existing_individual_products.count()} individual products (layout switching support)")
                                # Individual products are preserved for layout flexibility

                        except Exception as e:
                            logger.error(f"Error processing consolidated product (fallback): {str(e)}")
                    else:
                        logger.warning("No consolidated product found in payload, but consolidate=true. Keeping existing state.")

        else:
            # Handle individual products mode - use products array
            logger.info("Processing in individual products mode")
            line_items = analyzed_data.get('products', [])

            if line_items:
                logger.info(f"Processing {len(line_items)} individual products")
                update_analyzed_products(analyzed_bill, line_items, organization)

                # Keep any existing consolidated products for layout switching support
                existing_consolidated = TallyVendorConsolidatedProduct.objects.filter(vendor_bill_analyzed=analyzed_bill)
                if existing_consolidated.exists():
                    logger.info(f"Keeping {existing_consolidated.count()} consolidated products (layout switching support)")
                    # Consolidated products are preserved for layout flexibility

        return analyzed_bill


def find_or_create_vendor_ledger(vendor_name, vendor_data, organization):
    """Delegates to shared find_or_create_tally_vendor_ledger in helpers."""
    from .helpers import find_or_create_tally_vendor_ledger
    return find_or_create_tally_vendor_ledger(
        vendor_name, vendor_data, organization,
        default_parent_name="Sundry Creditors", create_default_parent=True
    )


def find_or_create_tax_ledger(ledger_name, tax_type, organization):
    """Delegates to shared find_or_create_tally_tax_ledger in helpers."""
    from .helpers import find_or_create_tally_tax_ledger
    return find_or_create_tally_tax_ledger(ledger_name, tax_type, organization)


def update_analyzed_products(analyzed_bill, line_items, organization):
    """
    Update existing products by item_id, or create new ones if item_id is missing/unknown.
    Keeps vendor_bill_analyzed FK to analyzed_bill.
    Calculates GST values based on gst_type from analyzed_bill.
    """

    # Map existing products by UUID string
    existing = {str(p.id): p for p in analyzed_bill.products.all()}
    updated_ids = set()

    for item in line_items or []:
        item_id = str(item.get('item_id')) if item.get('item_id') is not None else None

        if item_id and item_id in existing:
            product = existing[item_id]
            updated_ids.add(item_id)

            needs_update = False

            # Fields
            if 'item_name' in item and product.item_name != item.get('item_name'):
                product.item_name = item.get('item_name')
                needs_update = True
            if 'item_details' in item and product.item_details != item.get('item_details'):
                product.item_details = item.get('item_details')
                needs_update = True
            if 'price' in item:
                new_price = _to_decimal(item.get('price'), "0")
                if product.price != new_price:
                    product.price = new_price
                    needs_update = True
            if 'quantity' in item:
                new_qty = _to_int(item.get('quantity'), 0)
                if product.quantity != new_qty:
                    product.quantity = new_qty
                    needs_update = True
            if 'amount' in item:
                new_amount = _to_decimal(item.get('amount'), "0")
                if product.amount != new_amount:
                    product.amount = new_amount
                    needs_update = True
            if 'product_gst' in item:
                normalized_gst = normalize_product_gst(item.get('product_gst'))
                if product.product_gst != normalized_gst:
                    product.product_gst = normalized_gst
                    needs_update = True

            # Calculate GST values based on gst_type from analyzed_bill
            # This ensures GST is correctly distributed according to bill's GST type
            current_amount = _to_decimal(item.get('amount'), str(product.amount))
            current_product_gst = item.get('product_gst', product.product_gst)

            calc_igst, calc_cgst, calc_sgst = calculate_product_gst(
                current_amount,
                current_product_gst,
                analyzed_bill.gst_type
            )

            # Update GST values if calculated values differ
            if product.igst != calc_igst:
                product.igst = calc_igst
                needs_update = True
            if product.cgst != calc_cgst:
                product.cgst = calc_cgst
                needs_update = True
            if product.sgst != calc_sgst:
                product.sgst = calc_sgst
                needs_update = True

            # Tax ledger — prefer UUID over name (name collision +
            # sentinel-string mismatch was silently wiping the real
            # ledger on re-verify). Treat all blank/sentinel names
            # as "no change" so we never overwrite a good FK with a
            # bogus find_or_create_tax_ledger("No Purchase Ledger").
            _BLANK_LEDGER_SENTINELS = {"", "no tax ledger", "no purchase ledger", "none", "null"}
            tax_ledger_id = item.get('tax_ledger_id') or item.get('taxes')
            if tax_ledger_id:
                try:
                    from ..models import Ledger as _Ledger
                    new_tax_ledger = _Ledger.objects.get(id=tax_ledger_id, organization=organization)
                    if product.taxes_id != new_tax_ledger.id:
                        product.taxes = new_tax_ledger
                        needs_update = True
                except (_Ledger.DoesNotExist, ValueError):
                    logger.warning(
                        "update_analyzed_products: tax_ledger_id %s not found in org %s",
                        tax_ledger_id, organization.id,
                    )
            elif 'tax_ledger' in item:
                name = str(item.get('tax_ledger') or '').strip()
                if name.lower() not in _BLANK_LEDGER_SENTINELS:
                    current_name = str(product.taxes) if product.taxes else ""
                    if current_name != name:
                        tax_ledger = find_or_create_tax_ledger(name, 'Product Tax', organization)
                        if tax_ledger:
                            product.taxes = tax_ledger
                            needs_update = True

            # Per-line CGST / SGST / IGST tax ledger FKs (used for mixed-rate bills)
            for fk_field in ('cgst_ledger', 'sgst_ledger', 'igst_ledger'):
                if fk_field in item:
                    raw_id = item.get(fk_field)
                    new_id = str(raw_id) if raw_id else None
                    # Validate the UUID before write so a stray value
                    # doesn't crash save() with an IntegrityError.
                    if new_id:
                        try:
                            from ..models import Ledger as _Ledger
                            _Ledger.objects.only('id').get(
                                id=new_id, organization=organization,
                            )
                        except (_Ledger.DoesNotExist, ValueError):
                            logger.warning(
                                "Skipping %s=%s on product update — ledger "
                                "not found in org %s.",
                                fk_field, new_id, organization.id,
                            )
                            continue
                    current_id = str(getattr(product, f'{fk_field}_id') or '') or None
                    if current_id != new_id:
                        setattr(product, f'{fk_field}_id', new_id)
                        needs_update = True
            if needs_update:
                product.save()
                logger.info(f"Updated product {item_id} with GST type: {analyzed_bill.gst_type}")
            else:
                logger.info(f"No changes for product {item_id}")
        else:
            # Create new product
            product_amount = _to_decimal(item.get('amount'), "0")
            product_gst_str = normalize_product_gst(item.get('product_gst', '18%'))  # 🎯 Normalize GST value

            # Calculate GST values based on gst_type from analyzed_bill
            calc_igst, calc_cgst, calc_sgst = calculate_product_gst(
                product_amount,
                product_gst_str,
                analyzed_bill.gst_type
            )

            product = TallyVendorAnalyzedProduct(
                vendor_bill_analyzed=analyzed_bill,
                organization=organization,
                item_name=item.get('item_name'),
                item_details=item.get('item_details'),
                price=_to_decimal(item.get('price'), "0"),
                quantity=_to_int(item.get('quantity'), 0),
                amount=product_amount,
                product_gst=product_gst_str,  # 🎯 Already normalized above
                igst=calc_igst,
                cgst=calc_cgst,
                sgst=calc_sgst,
            )
            # Same guard as update path: prefer UUID, reject sentinel strings.
            _BLANK_LEDGER_SENTINELS = {"", "no tax ledger", "no purchase ledger", "none", "null"}
            tax_ledger_id = item.get('tax_ledger_id') or item.get('taxes')
            if tax_ledger_id:
                try:
                    from ..models import Ledger as _Ledger
                    product.taxes = _Ledger.objects.get(id=tax_ledger_id, organization=organization)
                except (_Ledger.DoesNotExist, ValueError):
                    pass
            else:
                name = str(item.get('tax_ledger') or '').strip()
                if name.lower() not in _BLANK_LEDGER_SENTINELS:
                    tax_ledger = find_or_create_tax_ledger(name, 'Product Tax', organization)
                    if tax_ledger:
                        product.taxes = tax_ledger

            for fk_field in ('cgst_ledger', 'sgst_ledger', 'igst_ledger'):
                raw_id = item.get(fk_field)
                if not raw_id:
                    continue
                # Validate the UUID belongs to an org ledger before
                # assigning — a bad ``fk_field_id`` write would crash
                # on save() with an integrity error otherwise.
                try:
                    from ..models import Ledger as _Ledger
                    _Ledger.objects.only('id').get(
                        id=str(raw_id), organization=organization,
                    )
                    setattr(product, f'{fk_field}_id', str(raw_id))
                except (_Ledger.DoesNotExist, ValueError):
                    logger.warning(
                        "Skipping %s=%s on product create — ledger not "
                        "found in org %s.",
                        fk_field, raw_id, organization.id,
                    )
            product.save()
            logger.info(
                f"Created new product (client item_id: {item.get('item_id')}) name={item.get('item_name') or 'Unknown'} "
                f"with GST type: {analyzed_bill.gst_type} (IGST: {calc_igst}, CGST: {calc_cgst}, SGST: {calc_sgst})")

    # Frontend is source of truth for what should exist: any existing
    # product whose id isn't in the incoming list gets deleted, in
    # BOTH consolidate and non-consolidate mode. The old "preserve
    # individual products in consolidate mode" rule left stale rows
    # after the user removed a line item — reload showed the deleted
    # line back on screen.
    #
    # In consolidate mode we ALSO rebuild the ``consolidated_products``
    # aggregation from the fresh product set so the aggregation table
    # reflects the current line-item state.
    products_to_delete = [
        product for existing_id, product in existing.items()
        if existing_id not in updated_ids
    ]
    if products_to_delete:
        for product in products_to_delete:
            logger.info(f"Deleting product {product.id}: {product.item_name or 'Unknown'}")
            product.delete()
        logger.info(
            "Deleted %d products not present in frontend payload (consolidate=%s)",
            len(products_to_delete),
            getattr(analyzed_bill, 'consolidate', False),
        )

    # Refresh consolidated aggregation if any change happened AND
    # consolidate mode is on. Simple approach: wipe + let the
    # verify path's consolidation build run on next open. Keeps the
    # aggregation table honest without duplicating consolidation
    # logic here.
    if products_to_delete and getattr(analyzed_bill, 'consolidate', False):
        try:
            TallyVendorConsolidatedProduct.objects.filter(
                vendor_bill_analyzed=analyzed_bill,
            ).delete()
            logger.info(
                "Cleared consolidated aggregation for bill %s — will "
                "regenerate on next consolidate toggle / verify.",
                analyzed_bill.id,
            )
        except Exception as exc:
            logger.warning(
                "Failed to clear consolidated aggregation for bill %s: %s",
                analyzed_bill.id, exc,
            )

    # Calculate deletion count for summary
    deletion_count = len(products_to_delete)

    logger.info(
        f"Product update summary: {len(updated_ids)} updated, "
        f"{len(line_items or []) - len(updated_ids)} created, "
        f"{deletion_count} deleted (consolidate mode: {getattr(analyzed_bill, 'consolidate', False)})"
    )


def get_structured_bill_data(analyzed_bill, organization):
    vendor_ledger = analyzed_bill.vendor
    analyzed_bill_products = analyzed_bill.products.all()
    bill_date_str = analyzed_bill.bill_date.strftime('%d-%m-%Y') if analyzed_bill.bill_date else None
    team_slug = organization.name if hasattr(organization, 'name') else str(organization.id)

    # Consolidated aggregation rows — mirrored to top-level
    # ``consolidate_prod`` so the FE can pre-populate the consolidated
    # products table on verify response without a second fetch.
    consolidated_rows = []
    try:
        for cp in analyzed_bill.consolidated_products.all():
            consolidated_rows.append({
                "id": str(cp.id),
                "item_id": str(cp.id),
                "item_name": cp.item_name,
                "item_details": cp.item_details,
                "tax_ledger": cp.taxes.name if cp.taxes else "",
                "tax_ledger_id": str(cp.taxes.id) if cp.taxes else None,
                "price": float(cp.price or 0),
                "quantity": int(cp.quantity or 0),
                "amount": float(cp.amount or 0),
                "product_gst": cp.product_gst,
                "igst": float(cp.igst or 0),
                "cgst": float(cp.cgst or 0),
                "sgst": float(cp.sgst or 0),
                "original_items_count": cp.original_items_count or 0,
            })
    except Exception:
        consolidated_rows = []

    return {
        "vendor": {
            "master_id": vendor_ledger.master_id if vendor_ledger and vendor_ledger.master_id else "",
            "name": vendor_ledger.name if vendor_ledger and vendor_ledger.name else "",
            # Alias so old FE (reads vendor_name) keeps working alongside
            # newer FE (reads .name).
            "vendor_name": vendor_ledger.name if vendor_ledger and vendor_ledger.name else "",
            "gst_in": vendor_ledger.gst_in if vendor_ledger and vendor_ledger.gst_in else "",
            "company": vendor_ledger.company if vendor_ledger and vendor_ledger.company else "",
            # UUID so the frontend can re-select the exact ledger row
            # on reload instead of guessing by name.
            "id": str(vendor_ledger.id) if vendor_ledger else None,
        },
        # Nested block kept for Tally-sync consumers that already read
        # from ``bill_details``. Never removed here for backward compat.
        "bill_details": {
            "bill_number": analyzed_bill.bill_no,
            "date": bill_date_str,
            "due_date": analyzed_bill.due_date.strftime('%d-%m-%Y') if analyzed_bill.due_date else None,
            "total_amount": float(analyzed_bill.total or 0),
            "company_id": team_slug,
        },
        # ---- flat mirror keys ----
        # FE detail page reads these top-level keys on verify response
        # to re-hydrate its form state. Introduced alongside the nested
        # ``bill_details`` (which stays for sync compat) so no reader
        # breaks either way.
        "bill_no": analyzed_bill.bill_no or "",
        "bill_date": bill_date_str,
        "due_date": (
            analyzed_bill.due_date.strftime('%d-%m-%Y')
            if analyzed_bill.due_date else None
        ),
        "total_amount": float(analyzed_bill.total or 0),
        "consolidate": bool(getattr(analyzed_bill, "consolidate", False)),
        "consolidate_prod": consolidated_rows,
        # Notes were saved but not returned — reload of a bill showed
        # blank notes even though the user had entered text.
        "note": analyzed_bill.note or "",
        # Response ships BOTH ledger name and UUID for every tax row.
        # Frontend prefers the UUID to auto-select the right dropdown
        # option (name alone can collide across parent groups).
        "taxes": {
            "igst": {
                "amount": float(analyzed_bill.igst or 0),
                "ledger": str(analyzed_bill.igst_taxes) if analyzed_bill.igst_taxes else "No Tax Ledger",
                "ledger_id": str(analyzed_bill.igst_taxes.id) if analyzed_bill.igst_taxes else None,
            },
            "cgst": {
                "amount": float(analyzed_bill.cgst or 0),
                "ledger": str(analyzed_bill.cgst_taxes) if analyzed_bill.cgst_taxes else "No Tax Ledger",
                "ledger_id": str(analyzed_bill.cgst_taxes.id) if analyzed_bill.cgst_taxes else None,
            },
            "sgst": {
                "amount": float(analyzed_bill.sgst or 0),
                "ledger": str(analyzed_bill.sgst_taxes) if analyzed_bill.sgst_taxes else "No Tax Ledger",
                "ledger_id": str(analyzed_bill.sgst_taxes.id) if analyzed_bill.sgst_taxes else None,
            },
            "discount": {
                "amount": float(analyzed_bill.discount or 0),
                "ledger": str(analyzed_bill.discount_taxes) if analyzed_bill.discount_taxes else "No Tax Ledger",
                "ledger_id": str(analyzed_bill.discount_taxes.id) if analyzed_bill.discount_taxes else None,
            },
            "cess": {
                "amount": float(analyzed_bill.cess or 0),
                "ledger": str(analyzed_bill.cess_taxes) if analyzed_bill.cess_taxes else "No Tax Ledger",
                "ledger_id": str(analyzed_bill.cess_taxes.id) if analyzed_bill.cess_taxes else None,
            },
            "freight": {
                "amount": float(analyzed_bill.freight or 0),
                "ledger": str(analyzed_bill.freight_taxes) if analyzed_bill.freight_taxes else "No Tax Ledger",
                "ledger_id": str(analyzed_bill.freight_taxes.id) if analyzed_bill.freight_taxes else None,
            },
            "round_off": {
                "amount": float(analyzed_bill.round_off or 0),
                "ledger": str(analyzed_bill.round_off_taxes) if analyzed_bill.round_off_taxes else "No Tax Ledger",
                "ledger_id": str(analyzed_bill.round_off_taxes.id) if analyzed_bill.round_off_taxes else None,
            }
        },
        "products": [
            {
                "item_id": str(item.id),  # <-- return item_id for future PATCHes
                "item_name": item.item_name,
                "item_details": item.item_details,
                "tax_ledger": item.taxes.name if item.taxes else "No Tax Ledger",
                # Explicit UUID lets the frontend match the dropdown by ID
                # instead of guessing from the display name (was issue in
                # detail.jsx:2399 — name-based lookup lost tax ledger on reload).
                "tax_ledger_id": str(item.taxes.id) if item.taxes else None,
                "price": float(item.price or 0),
                "quantity": int(item.quantity or 0),
                "amount": float(item.amount or 0),
                "product_gst": item.product_gst,
                "igst": float(item.igst or 0),
                "cgst": float(item.cgst or 0),
                "sgst": float(item.sgst or 0),
            }
            for item in analyzed_bill_products
        ],
    }


# ============================================================================
# Bill Sync View
# ✅
@extend_schema(
    summary="Sync Vendor Bill",
    description="Sync verified vendor bill with Tally system",
    request=BillSyncRequestSerializer,
    responses={200: BillSyncResponseSerializer},
    tags=['Tally Vendor Bills']
)
@api_view(['POST'])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def vendor_bill_sync(request, org_id):
    """Sync verified vendor bill with Tally"""
    serializer = BillSyncRequestSerializer(data=request.data)
    if not serializer.is_valid():
        return Response({
            'error': 'Invalid Sync Request',
            'message': 'The sync request data is invalid. Please ensure the bill_id is provided and valid.',
            'details': serializer.errors,
            'error_code': 'INVALID_SYNC_REQUEST'
        }, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

    bill_id = serializer.validated_data['bill_id']
    organization = get_organization_from_request(request, org_id)

    try:
        bill = TallyVendorBill.objects.alive().get(id=bill_id, organization=organization)

        # Handle potential multiple analyzed bills - use the most recent one
        try:
            analyzed_bill = TallyVendorAnalyzedBill.objects.get(selected_bill=bill)
        except TallyVendorAnalyzedBill.MultipleObjectsReturned:
            # Multiple analyzed bills exist - use the most recent one and log warning
            logger.warning(f"Multiple TallyVendorAnalyzedBill found for bill {bill_id} in sync, using the most recent one")
            analyzed_bill = TallyVendorAnalyzedBill.objects.filter(
                selected_bill=bill
            ).order_by('-created_at').first()

            # Optionally clean up duplicate records (keep only the most recent)
            duplicate_bills = TallyVendorAnalyzedBill.objects.filter(
                selected_bill=bill
            ).exclude(id=analyzed_bill.id)

            if duplicate_bills.exists():
                duplicate_count = duplicate_bills.count()
                duplicate_bills.delete()
                logger.info(f"Cleaned up {duplicate_count} duplicate TallyVendorAnalyzedBill records for bill {bill_id} during sync")

    except (TallyVendorBill.DoesNotExist, TallyVendorAnalyzedBill.DoesNotExist):
        return Response({
            'error': 'Bill or Analysis Data Not Found',
            'message': f'Bill with ID {bill_id} or its analyzed data not found. Please ensure the bill exists and has been analyzed.',
            'error_code': 'BILL_OR_ANALYSIS_NOT_FOUND'
        }, status=status.HTTP_404_NOT_FOUND)

    if bill.status != TallyVendorBill.BillStatus.VERIFIED:
        return Response({
            'error': 'Bill Not Ready for Sync',
            'message': f'Bill must be in "Verified" status to sync with Tally. Current status: {bill.status}. Please verify the bill first.',
            'current_status': bill.status,
            'required_status': 'Verified',
            'error_code': 'BILL_NOT_VERIFIED'
        }, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

    # Master-sync check is now ADVISORY, not a hard block.
    # Old behavior: 409 refused sync until every BM-created ledger was
    # confirmed by Tally callback. That left users stuck when Tally TCP
    # was offline or slow. New behavior: always accept the sync into
    # BillMunshi's queue, mark bill status=SYNCED but keep
    # tally_synced=False. The Tally TCP polls both master + bill
    # queues; the callback flips tally_synced=True on success or writes
    # an error message on failure. Frontend reads bill.tally_synced +
    # bill.tally_sync_message to show the final state.
    from .bill_sync_guard import find_pending_masters
    pending_masters = find_pending_masters(analyzed_bill)

    try:
        sync_data = get_structured_bill_data(analyzed_bill, organization)

        bill.status = TallyVendorBill.BillStatus.SYNCED
        bill.save(update_fields=['status'])

        # Compose a human-friendly status the FE surfaces:
        #   "pending_tally"  → BillMunshi has queued the bill; Tally has not
        #                      yet confirmed it (either masters pending or
        #                      Tally TCP hasn't polled yet).
        #   "confirmed"      → callback already fired (rare on the sync
        #                      request itself; usually applies on later
        #                      reads).
        tally_state = "confirmed" if bill.tally_synced else "pending_tally"

        try:
            sync_response = vendor_bill_sync_external_handler(sync_data, org_id, organization)
            return Response({
                "message": (
                    "Bill queued for Tally sync"
                    if tally_state == "pending_tally"
                    else "Bill synced to Tally"
                ),
                "bill_id": str(bill_id),
                "status": "Synced",
                "tally_sync_status": tally_state,
                "pending_masters": pending_masters,
                "pending_masters_count": len(pending_masters),
                "data": sync_response
            }, status=status.HTTP_200_OK)

        except Exception as sync_error:
            logger.warning(f"External sync failed but bill status updated: {str(sync_error)}")
            return Response({
                "message": "Bill queued for Tally sync (external handler failed)",
                "bill_id": str(bill_id),
                "status": "Synced",
                "tally_sync_status": tally_state,
                "pending_masters": pending_masters,
                "pending_masters_count": len(pending_masters),
                "sync_data": sync_data,
                "external_sync_error": str(sync_error)
            }, status=status.HTTP_200_OK)

    except Exception as e:
        logger.error(f"Bill sync failed: {str(e)}")
        return Response({
            'error': 'Bill Sync Failed',
            'message': 'The bill sync process encountered an error. This could be due to Tally system connectivity issues or invalid bill data. Please verify your data and try again.',
            'details': str(e),
            'error_code': 'SYNC_FAILED'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


def vendor_bill_sync_external_handler(sync_data, org_id, organization):
    """Handle external sync with the provided payload"""
    try:
        # Log the sync attempt
        logger.info(f"External sync handler called for organization {organization.id}")
        # logger.info(f"Sync data: {json.dumps(sync_data, indent=2)}")

        # Here you can add any external API calls or processing
        # For now, we'll just return a success response
        return sync_data

    except Exception as e:
        logger.error(f"External sync handler failed: {str(e)}")
        raise Exception(f"External sync failed: {str(e)}")


# ============================================================================
# Delete Vendor Bill
# ✅
@extend_schema(
    summary="Delete Vendor Bill",
    description="Delete a vendor bill and its associated file",
    responses={204: None},
    tags=['Tally Vendor Bills']
)
@api_view(['DELETE'])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def vendor_bill_delete(request, org_id, bill_id):
    """Delete vendor bill."""
    from .bill_helpers import bill_delete_base
    return bill_delete_base(request, org_id, bill_id, TallyVendorBill)


# ============================================================================
# Tally TCP Integration Views
# ✅
@extend_schema(
    summary="Get All Synced Bills",
    description="Get all synced bills with their products for the organization",
    responses={200: BillSyncResponseSerializer(many=True)},
    tags=['Tally TCP']
)
@api_view(['GET'])
@permission_classes([OrganizationAPIKeyOrBearerToken])
def vendor_bills_sync_list(request, org_id):
    """Get all synced bills with their products"""
    logger.info("vendor_bills_sync_list called")
    # 🔹 Log caller info & headers
    try:
        logger.info(
            "vendor_bills_sync_list called",
            extra={
                "path": request.get_full_path(),
                "method": request.method,
                "ip": get_client_ip(request),
                "headers": dict(request.headers),  # DRF >=3.12
            }
        )
    except Exception as e:
        logger.warning(f"Failed to log request headers: {e}")

    # Enhanced debug logging
    logger.info(f"Processing org_id from URL: {org_id}")

    # For API Key auth, organization might be in request.organization
    if hasattr(request, 'organization'):
        organization = request.organization
        logger.info(f"Using organization from API Key: {organization.id}")
    else:
        # Fallback to getting organization from org_id
        organization = get_organization_from_request(request, org_id)
        logger.info(f"Using organization from request: {organization.id if organization else None}")

    if not organization:
        logger.error("Organization not found")
        return Response({
            'error': 'Organization Access Denied',
            'message': f'Organization with ID {org_id} not found or you do not have access to it. Please check the organization ID and your API key permissions.',
            'error_code': 'ORG_NOT_FOUND'
        }, status=status.HTTP_404_NOT_FOUND)

    # Modified organization validation for API Key authentication
    # Always trust the organization from API Key or token auth and skip validation
    # This avoids string comparison issues with UUIDs
    logger.info(f"Proceeding with organization {organization.id}")

    logger.info(f"Querying bills for organization: {organization.id}")
    analyzed_bills = (
        TallyVendorAnalyzedBill.objects.filter(
            organization=organization,
            selected_bill__status=TallyVendorBill.BillStatus.SYNCED,
            selected_bill__tally_synced=False,
            # Never hand a trashed bill to the Tally TCP bridge.
            selected_bill__is_deleted=False,
        )
        .select_related('selected_bill', 'vendor', 'igst_taxes', 'cgst_taxes', 'sgst_taxes')
        .prefetch_related(
            'products__taxes',
            'products__cgst_ledger',
            'products__sgst_ledger',
            'products__igst_ledger',
            # ``TallyVendorConsolidatedProduct`` only has a single
            # ``taxes`` FK (purchase-side); it has NO per-line CGST/SGST/
            # IGST ledgers. Consolidated products fall back to bill-level
            # tax ledgers at emit time (see prepare_sync_data). Adding
            # non-existent FKs here would crash Django prefetch validation.
            'consolidated_products__taxes',
        )
        .order_by('-created_at')
    )

    bills_count = analyzed_bills.count()
    logger.info(f"Found {bills_count} synced bills")

    bills_data = []
    for analyzed_bill in analyzed_bills:
        sync_data = prepare_sync_data(analyzed_bill, organization)
        bills_data.append(sync_data["data"])

    # Tally's TDL/TCP connector consumes XML natively and its text parser
    # choked on JSON (the `\"` escape for `"` inside item names broke ingestion).
    # XML sidesteps the issue: `"` is a normal character inside element
    # content, no escaping needed.
    xml_payload = _sync_data_to_xml(bills_data)
    return HttpResponse(xml_payload, content_type='application/xml; charset=utf-8')


def get_client_ip(request):
    """Extract client IP (supports reverse proxy headers)."""
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def _clean_tally_text(value):
    """Sanitize text for Tally XML sync: collapse newlines/tabs/carriage-returns
    to single spaces. Double-quotes pass through unchanged — inside XML element
    content `"` is a normal character and does not need escaping, so Tally will
    receive values like `1" Cello Tape` as-is. The XML serializer handles
    `<`, `>`, and `&` escaping automatically."""
    if value is None:
        return None
    text = str(value).replace('\r', ' ').replace('\n', ' ').replace('\t', ' ')
    return ' '.join(text.split())


def _sync_data_to_xml(bills_data):
    """Convert the list of sync-bill dicts built by ``prepare_sync_data`` into a
    Tally-compatible XML payload.

    Schema (a single flat ``<ledgers>`` collection, each child a
    ``<ledger>`` entry — Tally TDL just iterates):

        <data>
          <bill>
            <bill_no>MS/2025-26/3054</bill_no>
            <bill_date>20-12-2025</bill_date>
            <vendor>AGGARWAL TRADE LINK</vendor>
            <company>Spectrum Poly Pack and Packaging</company>
            <total_amount>3776.00</total_amount>
            <notes>...</notes>
            <ledgers>
              <ledger>
                <amount>288.00</amount>
                <ledger>CGST (ITC) @ 9%</ledger>
                <rate>18%</rate>
              </ledger>
              <ledger>
                <amount>288.00</amount>
                <ledger>SGST (ITC) @ 9%</ledger>
                <rate>18%</rate>
              </ledger>
              <!-- IGST / discount / cess / freight / round_off entries
                   share the same shape. ``<rate>`` is present only on
                   the GST entries (CGST/SGST/IGST). -->
            </ledgers>
            <items>
              <item>
                <name>1" SELF ADHESIVE TAPE</name>
                <details>Trophies 9502 Medium</details>
                <purchase_ledger>PURCHASE ACCOUNTS @18%</purchase_ledger>
                <price>400.00</price>
                <quantity>8</quantity>
                <amount>3200.00</amount>
              </item>
            </items>
          </bill>
        </data>

    Mixed-rate bills produce multiple ``<ledger>`` entries — one per
    distinct ledger. Tally TDL iterates and posts each as its own voucher
    line, classifying it by the ``<ledger>`` name.

    ElementTree handles XML escaping of ``<``, ``>``, ``&``. Inner
    double-quotes stay literal inside element content (XML spec) so values
    like ``1" SELF ADHESIVE TAPE`` are preserved as-is.
    """
    from xml.etree import ElementTree as ET

    def _set_scalar(parent_elem, key, value):
        child = ET.SubElement(parent_elem, key)
        child.text = '' if value is None else str(value)

    def _emit_ledger_entry(parent_elem, entry):
        """Emit ONE ``<ledger>`` row. ``entry`` shape:
            {"amount": "288.00", "ledger": "...",
             "rate": "18%", "debit_or_credit": "debit"}
        ``rate`` is optional — only present on GST (cgst/sgst/igst) entries.
        ``debit_or_credit`` is optional — only present on journal-style
        bills (expense) where each ledger needs an explicit DR/CR.
        Purchase-voucher bills (vendor) omit it (DR/CR is implicit).
        """
        ledger_elem = ET.SubElement(parent_elem, "ledger")
        _set_scalar(ledger_elem, "amount", entry.get("amount"))
        _set_scalar(ledger_elem, "ledger", entry.get("ledger"))
        if entry.get("rate"):
            _set_scalar(ledger_elem, "rate", entry["rate"])
        if entry.get("debit_or_credit"):
            _set_scalar(ledger_elem, "debit_or_credit", entry["debit_or_credit"])

    root = ET.Element('data')
    for bill in bills_data:
        bill_elem = ET.SubElement(root, 'bill')
        for key, value in bill.items():
            if key == 'ledgers' and isinstance(value, list):
                ledgers_elem = ET.SubElement(bill_elem, 'ledgers')
                for entry in value:
                    _emit_ledger_entry(ledgers_elem, entry)
            elif key == 'items' and isinstance(value, list):
                # Emit <items> only when there IS something to put in it.
                # In Accounting-Invoice mode (Inventory Sync = OFF) the
                # list is empty; Tally ignores an empty <items></items>
                # anyway, so we drop the tag entirely for a cleaner
                # payload — the presence of <items> alone is what tells
                # the TDL to switch to Item-Invoice mode.
                if not value:
                    continue
                items_elem = ET.SubElement(bill_elem, 'items')
                for item in value:
                    item_elem = ET.SubElement(items_elem, 'item')
                    for ik, iv in item.items():
                        _set_scalar(item_elem, ik, iv)
            else:
                _set_scalar(bill_elem, key, value)

    # ``xml_declaration=False`` drops the ``<?xml version='1.0' encoding='utf-8'?>``
    # prolog. Tally's TDL/TCP parser doesn't need it, and the client asked for
    # the response to start directly with ``<data>``.
    return ET.tostring(root, encoding='utf-8', xml_declaration=False).decode('utf-8')


def prepare_sync_data(analyzed_bill, organization):
    """Build the simplified sync payload for Tally TCP.

    Top-level shape:
        {
          "bill_no": str, "bill_date": "DD-MM-YYYY",
          "vendor": str, "company": str,
          "total_amount": "3776.00",
          "notes": str,
          "ledgers": [
            # Flat list — one entry per (tax_type, ledger) bucket for GST
            # entries, single bill-level entry for discount/cess/freight/
            # round_off. ``rate`` is present only on GST entries.
            {"amount": "288.00", "ledger": "CGST (ITC) @ 9%", "rate": "18%"},
            {"amount": "288.00", "ledger": "SGST (ITC) @ 9%", "rate": "18%"},
            {"amount": "100.00", "ledger": "Discount Received"},
            ...
          ],
          "items": [   # NO tax fields per item
            {"name", "details", "purchase_ledger",
             "price", "quantity", "amount"},
          ]
        }

    Mixed-rate bills produce multiple cgst/sgst/igst entries (one per
    distinct ledger). Discount/cess/freight/round_off are bill-level
    single entries. Any tax with amount == 0 is dropped entirely.
    """
    vendor_ledger = analyzed_bill.vendor
    bill_date_str = (
        analyzed_bill.bill_date.strftime('%d-%m-%Y') if analyzed_bill.bill_date else None
    )
    company_name = organization.name if hasattr(organization, 'name') else str(organization.id)

    vendor_name = vendor_ledger.name if vendor_ledger and vendor_ledger.name else "Unknown Vendor"
    bill_url = f"https://billmunshi.com/tally/vendor-bill/{analyzed_bill.selected_bill.id}"
    notes_message = f"Bill from {vendor_name} entered via BillMunshi {bill_url}"

    Q2 = Decimal('0.01')

    def _money(value):
        """Coerce to a 2-dp Decimal. Empty/invalid → 0.00."""
        if value is None or value == '':
            return Decimal('0.00')
        try:
            return Decimal(str(value)).quantize(Q2)
        except (InvalidOperation, ValueError, TypeError):
            return Decimal('0.00')

    def _fmt_money(value):
        """Render Decimal as ``"288.00"`` — never ``"288.0"`` and never float."""
        return f"{_money(value):.2f}"

    # ------------------------------------------------------------------
    # Inventory toggle (Settings → Tally Integration → "Inventory Sync").
    # When OFF the voucher is emitted in "Accounting Invoice" mode:
    #   * <items> is left empty (Tally treats it as a plain accounting
    #     voucher, no stock update).
    #   * The taxable amount rolls up into a single <ledger> row using
    #     the products' purchase-ledger (or the first item's if mixed).
    # When ON (default from a user's PoV, though the DB default is
    # False for legacy reasons) the existing per-item shape is emitted.
    # ------------------------------------------------------------------
    from ..models import TallyConfig
    tally_config = TallyConfig.objects.filter(organization=organization).first()
    use_inventory = bool(getattr(tally_config, 'tally_product_allow_sync', False))

    # ------------------------------------------------------------------
    # Items: ZERO tax fields here. All tax info lives in the <taxes>
    # block which is grouped by (type, ledger) below.
    # ------------------------------------------------------------------
    items_payload = []
    line_records = []  # used for grouping taxes; (amount_decimal, gst_rate, igst_ledger, igst_amt, cgst_ledger, cgst_amt, sgst_ledger, sgst_amt)

    use_consolidated = bool(getattr(analyzed_bill, 'consolidate', False))
    if use_consolidated:
        try:
            source_lines = list(analyzed_bill.consolidated_products.all())
            if not source_lines:
                # Empty consolidation table — silently fall back to individual.
                use_consolidated = False
        except Exception as exc:
            logger.error(
                f"Error reading consolidated products for bill {analyzed_bill.bill_no}: {exc}"
            )
            use_consolidated = False

    if not use_consolidated:
        source_lines = list(analyzed_bill.products.all())

    logger.info(
        "prepare_sync_data: bill=%s, lines=%d, source=%s, inventory=%s",
        analyzed_bill.bill_no, len(source_lines),
        'consolidated' if use_consolidated else 'individual',
        'on' if use_inventory else 'off',
    )

    # Accounting-invoice mode rollup: bucket product amounts by their
    # purchase ledger so a mixed-rate bill (some 18%, some 28% items)
    # still emits ONE <ledger> row per distinct purchase-ledger.
    # Only used when ``use_inventory=False``.
    purchase_rollup = {}  # {purchase_ledger_name: Decimal_total}

    for line in source_lines:
        # The "purchase ledger" is the dr-side ledger that the bill amount
        # is booked against (e.g. ``PURCHASE ACCOUNTS @18%``). It is the
        # FK named ``taxes`` on the product model.
        purchase_ledger_name = (
            str(line.taxes) if getattr(line, 'taxes', None) else "No Purchase Ledger"
        )

        if use_inventory:
            # Item-invoice mode — per-item <item> rows with the purchase
            # ledger attached to each one.
            items_payload.append({
                "name": _clean_tally_text(getattr(line, 'item_name', '')) or "",
                "details": _clean_tally_text(getattr(line, 'item_details', '')) or "",
                "purchase_ledger": purchase_ledger_name,
                "price": _fmt_money(getattr(line, 'price', 0)),
                "quantity": int(getattr(line, 'quantity', 0) or 0),
                "amount": _fmt_money(getattr(line, 'amount', 0)),
            })
        else:
            # Accounting-invoice mode — collapse into a purchase-ledger
            # rollup that will be prepended to the ``ledgers_payload`` below.
            purchase_rollup[purchase_ledger_name] = (
                purchase_rollup.get(purchase_ledger_name, Decimal('0.00'))
                + _money(getattr(line, 'amount', 0))
            )

        # Capture line-level tax info for the grouping pass below.
        # Per-product GST ledger FKs only exist on individual products,
        # not consolidated. Fall back to bill-level ledgers when missing.
        line_records.append({
            "rate": getattr(line, 'product_gst', None) or '',
            "igst_amt": _money(getattr(line, 'igst', 0)),
            "cgst_amt": _money(getattr(line, 'cgst', 0)),
            "sgst_amt": _money(getattr(line, 'sgst', 0)),
            "igst_ledger": (
                getattr(line, 'igst_ledger', None) or analyzed_bill.igst_taxes
            ),
            "cgst_ledger": (
                getattr(line, 'cgst_ledger', None) or analyzed_bill.cgst_taxes
            ),
            "sgst_ledger": (
                getattr(line, 'sgst_ledger', None) or analyzed_bill.sgst_taxes
            ),
        })

    # ------------------------------------------------------------------
    # GST grouping: collapse per-line cgst/sgst/igst into one entry per
    # (tax_type, ledger). Mixed-rate bills emit multiple entries of the
    # same tax_type — Tally TDL must iterate.
    # ------------------------------------------------------------------
    gst_buckets = {}  # key=(tax_type, ledger_id_or_name) -> aggregated dict

    def _bucket_for(tax_type, ledger):
        if ledger is None:
            return None
        # Prefer FK PK for bucket key (handles same name across orgs);
        # fall back to ledger name when only a string is present.
        ledger_id = getattr(ledger, 'pk', None) or getattr(ledger, 'id', None) or str(ledger)
        ledger_name = str(ledger)
        key = (tax_type, ledger_id)
        if key not in gst_buckets:
            gst_buckets[key] = {
                "type": tax_type,
                "amount": Decimal('0.00'),
                "ledger": ledger_name,
                "rates": [],  # collected for `rate` attribute
            }
        return gst_buckets[key]

    for rec in line_records:
        for tax_type in ("igst", "cgst", "sgst"):
            amt = rec[f"{tax_type}_amt"]
            if amt == 0:
                continue
            bucket = _bucket_for(tax_type, rec[f"{tax_type}_ledger"])
            if bucket is None:
                logger.warning(
                    "Line has %s amount %s but no ledger — skipping in payload",
                    tax_type, amt,
                )
                continue
            bucket["amount"] += amt
            if rec["rate"]:
                bucket["rates"].append(rec["rate"])

    # Sort GST buckets in a stable, predictable order before flattening:
    # cgst → sgst → igst, ascending by rate so mixed-rate bills read
    # "18% then 28%". The Tally side doesn't depend on the order, but a
    # stable order keeps the payload diff-friendly for QA.
    _gst_order = {"cgst": 0, "sgst": 1, "igst": 2}

    def _rate_num(s):
        try:
            return float(str(s).replace('%', '').strip())
        except (ValueError, TypeError):
            return 0.0

    sorted_gst_buckets = sorted(
        gst_buckets.values(),
        key=lambda b: (
            _gst_order.get(b["type"], 99),
            _rate_num(max(set(b["rates"]), key=b["rates"].count) if b["rates"] else ""),
        ),
    )

    ledgers_payload = []
    # When inventory sync is OFF we prepend a <ledger> row per distinct
    # purchase ledger — this is what makes the payload valid for
    # Tally's "Accounting Invoice" voucher mode (no stock update).
    #
    # ``"No Purchase Ledger"`` is a sentinel from lines that had no
    # taxes FK — sending it as a real ledger name gets rejected by
    # Tally ("Ledger not found"). Skip those rows and log so the
    # operator can fix the mapping.
    if not use_inventory:
        _BLANK = {"", "no purchase ledger", "no tax ledger", "none"}
        for pledger_name, total in purchase_rollup.items():
            if total == 0:
                continue
            if (pledger_name or "").strip().lower() in _BLANK:
                logger.warning(
                    "Sync inventory-OFF: skipping ₹%s with blank purchase ledger "
                    "for bill %s — configure a purchase ledger on the line item.",
                    _fmt_money(total), analyzed_bill.bill_no,
                )
                continue
            ledgers_payload.append({
                "amount": _fmt_money(total),
                "ledger": pledger_name,
            })

    for entry in sorted_gst_buckets:
        # Pick the dominant rate seen across grouped lines for the
        # informational `<rate>` element. (Within one ledger bucket
        # the rate is normally identical — multiple values would only
        # show up in malformed data.)
        rate_str = ""
        if entry["rates"]:
            rate_str = max(set(entry["rates"]), key=entry["rates"].count)
        ledgers_payload.append({
            "amount": _fmt_money(entry["amount"]),
            "ledger": entry["ledger"],
            "rate": rate_str,
        })

    # ------------------------------------------------------------------
    # Bill-level extras: discount / cess / freight / round_off.
    # Single entry each, dropped when zero. ``round_off`` may be negative.
    # No ``rate`` for these — they are flat values, classified by the
    # ledger name on the Tally side.
    #
    # ``discount`` is emitted with a NEGATED amount (e.g. -100.00) so
    # the Tally side can post it directly without flipping the sign in
    # TDL. The model stores discount as a positive number representing
    # "amount taken off the bill", so the wire format inverts it.
    # ------------------------------------------------------------------
    extras = (
        ("discount", analyzed_bill.discount, analyzed_bill.discount_taxes),
        ("cess", analyzed_bill.cess, analyzed_bill.cess_taxes),
        ("freight", analyzed_bill.freight, analyzed_bill.freight_taxes),
        ("round_off",
         getattr(analyzed_bill, 'round_off', 0),
         getattr(analyzed_bill, 'round_off_taxes', None)),
    )
    for tax_type, amount, ledger in extras:
        amt = _money(amount)
        if amt == 0:
            continue
        if tax_type == "discount":
            amt = -amt
        ledgers_payload.append({
            "amount": _fmt_money(amt),
            "ledger": str(ledger) if ledger else "No Tax Ledger",
        })

    bill_data = {
        # ``id`` is the BillMunshi bill UUID. The Tally TCP / TDL connector
        # echoes it back via the ``tally_status`` callback API so the
        # backend can flag the right bill as posted (``tally_synced=True``)
        # without having to match on bill_no + vendor + date heuristics.
        "id": str(analyzed_bill.selected_bill.id),
        "bill_no": analyzed_bill.bill_no,
        "bill_date": bill_date_str,
        "voucher_type": "Purchase",
        # ``vendor`` is the tag the TDL contract documents (see
        # docs/tally-master-sync.md). ``vendor_name`` was the legacy
        # key — kept as an alias so an older TDL parsing that value
        # doesn't break during rollout.
        "vendor": vendor_name,
        "vendor_name": vendor_name,
        "company": company_name,
        "total_amount": _fmt_money(analyzed_bill.total),
        "notes": notes_message,
        "ledgers": ledgers_payload,
        "items": items_payload,
    }

    return {"data": bill_data}


# ============================================================================
# Bill Moving Between Modules Functionality (Tally)
# ============================================================================

@extend_schema(
    summary="Sync Bill to External System",
    description="Accept bill data payload for external system sync",
    responses={
        200: OpenApiResponse(description="Payload accepted successfully"),
        400: OpenApiResponse(description="Invalid payload")
    },
    tags=['Tally TCP']
)
@api_view(['POST'])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def vendor_bill_sync_external(request, org_id):
    """Accept bill payload for external system sync"""
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
                'message': 'No payload data provided for external sync. Please provide the bill data to be processed.',
                'error_code': 'NO_PAYLOAD_PROVIDED'
            },
            status=status.HTTP_422_UNPROCESSABLE_ENTITY
        )

    try:
        # Log the received payload
        logger.info(f"External sync received payload for organization {organization.id}")
        logger.info(f"Payload: {json.dumps(payload, indent=2)}")

        # Here you can process the payload as needed
        # For now, we'll just acknowledge receipt

        return Response({
            'message': 'Payload received and processed successfully',
            'organization_id': str(organization.id),
            'payload_received': True,
            'timestamp': datetime.now().isoformat()
        }, status=status.HTTP_200_OK)

    except Exception as e:
        logger.error(f"External sync failed: {str(e)}")
        return Response({
            'error': 'External Sync Processing Failed',
            'message': 'There was an error processing the external sync payload. This could be due to invalid data format or system issues.',
            'details': str(e),
            'error_code': 'EXTERNAL_SYNC_FAILED'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)



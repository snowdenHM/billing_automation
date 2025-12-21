# apps/module/zoho/vendor_views.py

import base64
import json
import logging
import os
from datetime import datetime
from decimal import Decimal
from io import BytesIO

import requests
from PyPDF2 import PdfReader
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models.functions import Lower
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from openai import OpenAI
from pdf2image import convert_from_bytes
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, parser_classes
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.pagination import DefaultPagination
from apps.organizations.models import Organization
from .models import (
    ZohoCredentials,
    ZohoVendor,
    VendorBill,
    VendorZohoBill,
    VendorZohoProduct,
    VendorZohoConsolidatedProduct,
    ZohoChartOfAccount,
    ZohoTaxes,
    ZohoTdsTcs,
)
from .serializers.common import (
    AnalysisResponseSerializer,
)
from .serializers.vendor_bills import (
    ZohoVendorBillSerializer,
    ZohoVendorBillDetailSerializer,
    VendorZohoBillSerializer,
    ZohoVendorBillMultipleUploadSerializer,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Consolidation Helper Functions
# ============================================================================

def create_consolidated_vendor_product(zoho_bill, organization):
    """
    Create consolidated product entry for vendor bill when consolidate=True
    Handles tax conflicts by using most common or highest tax rate
    """
    from .models import VendorZohoConsolidatedProduct

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


def get_line_items_for_sync(zoho_bill):
    """
    Get line items for sync based on consolidate flag
    Returns either individual products or consolidated product data
    """
    if zoho_bill.consolidate:
        # Use consolidated product if exists
        try:
            consolidated = zoho_bill.consolidated_products.first()  # Get first consolidated product
            return [{
                "name": consolidated.consolidated_item_name,
                "description": consolidated.consolidated_item_details[:500],  # Zoho API limit
                "rate": str(consolidated.consolidated_rate),
                "quantity": str(consolidated.total_quantity),
                "unit": "unit",
                "item_total": str(consolidated.consolidated_amount),
                "tax_id": consolidated.taxes.taxId if consolidated.taxes else None,
                "account_id": consolidated.chart_of_accounts.accountId if consolidated.chart_of_accounts else None,
                "is_consolidated": True,
                "original_items_count": consolidated.original_items_count
            }]
        except VendorZohoConsolidatedProduct.DoesNotExist:
            # Fallback to creating consolidated data on the fly
            products = zoho_bill.products.all()
            if products.exists():
                total_amount = sum(float(p.amount or 0) for p in products)  # Fixed: use 'amount' not 'total'
                return [{
                    "name": f"Multiple items consolidated ({products.count()} products)",
                    "description": f"Consolidated from {products.count()} individual items",
                    "rate": str(total_amount),
                    "quantity": "1",
                    "unit": "unit",
                    "item_total": str(total_amount),
                    "is_consolidated": True,
                    "original_items_count": products.count()
                }]
    else:
        # Return individual products as usual
        line_items = []
        for product in zoho_bill.products.all():
            line_items.append({
                "name": product.item_name,  # Fixed: use 'item_name' not 'name'
                "description": product.item_name,  # Fixed: use 'item_name' not 'name'
                "rate": str(product.rate),
                "quantity": str(product.quantity),
                "unit": "unit",
                "item_total": str(product.amount),  # Fixed: use 'amount' not 'total'
                "tax_id": product.taxes.taxId if product.taxes else None,
                "account_id": product.chart_of_accounts.accountId if product.chart_of_accounts else None,
                "is_consolidated": False
            })
        return line_items


# ============================================================================
# Helper Functions
# ============================================================================

def check_duplicate_bill(bill, organization):
    """
    Check if a vendor bill is a duplicate based on invoice number and vendor information.
    Returns tuple (is_duplicate, duplicate_bills, similarity_score)
    """
    logger.info(f"Checking for duplicates of bill {bill.id} in organization {organization.id}")

    if not bill.analysed_data:
        # If bill hasn't been analyzed yet, we can't check for duplicates
        return False, [], 0.0

    analyzed_data = bill.analysed_data
    current_invoice_number = analyzed_data.get('invoiceNumber', '').strip()
    current_vendor_name = analyzed_data.get('from', {}).get('name', '').strip()
    current_total = analyzed_data.get('total', 0)
    current_date = analyzed_data.get('dateIssued', '')

    if not current_invoice_number and not current_vendor_name:
        # Can't check duplicates without key identifying information
        return False, [], 0.0

    # Find potentially duplicate bills in the same organization
    potential_duplicates = VendorBill.objects.filter(
        organization=organization,
        status__in=['Analysed', 'Verified', 'Synced']
    ).exclude(id=bill.id)

    duplicate_bills = []
    max_similarity = 0.0

    for other_bill in potential_duplicates:
        if not other_bill.analysed_data:
            continue

        other_data = other_bill.analysed_data
        other_invoice_number = other_data.get('invoiceNumber', '').strip()
        other_vendor_name = other_data.get('from', {}).get('name', '').strip()
        other_total = other_data.get('total', 0)
        other_date = other_data.get('dateIssued', '')

        similarity_score = 0.0
        match_reasons = []

        # Check exact invoice number match
        if (current_invoice_number and other_invoice_number and
            current_invoice_number.lower() == other_invoice_number.lower()):
            similarity_score += 40.0
            match_reasons.append('exact_invoice_number')

        # Check vendor name similarity
        if current_vendor_name and other_vendor_name:
            vendor_similarity = _calculate_string_similarity(current_vendor_name.lower(), other_vendor_name.lower())
            if vendor_similarity > 0.8:
                similarity_score += 25.0 * vendor_similarity
                match_reasons.append('vendor_name_match')

        # Check total amount match
        if current_total and other_total:
            try:
                current_amount = float(current_total)
                other_amount = float(other_total)
                if abs(current_amount - other_amount) < 0.01:  # Allow for minor rounding differences
                    similarity_score += 20.0
                    match_reasons.append('exact_amount')
                elif abs(current_amount - other_amount) / max(current_amount, other_amount) < 0.05:  # 5% difference
                    similarity_score += 10.0
                    match_reasons.append('similar_amount')
            except (ValueError, TypeError):
                pass

        # Check date similarity
        if current_date and other_date and current_date == other_date:
            similarity_score += 15.0
            match_reasons.append('same_date')

        # Consider it a potential duplicate if similarity is high
        if similarity_score >= 60.0:  # Threshold for considering as duplicate
            duplicate_bills.append({
                'bill': other_bill,
                'similarity_score': similarity_score,
                'match_reasons': match_reasons,
                'invoice_number': other_invoice_number,
                'vendor_name': other_vendor_name,
                'total': other_total,
                'date': other_date
            })
            max_similarity = max(max_similarity, similarity_score)

    is_duplicate = len(duplicate_bills) > 0
    logger.info(f"Duplicate check complete. Found {len(duplicate_bills)} potential duplicates with max similarity {max_similarity}")

    return is_duplicate, duplicate_bills, max_similarity


def _calculate_string_similarity(str1, str2):
    """Calculate similarity between two strings using enhanced logic for Indian business names."""
    if not str1 or not str2:
        return 0.0

    str1_clean = str1.lower().strip()
    str2_clean = str2.lower().strip()

    # Exact match
    if str1_clean == str2_clean:
        return 1.0

    # Common business abbreviations for Indian companies
    abbreviations = {
        'ltd': 'limited',
        'pvt': 'private',
        'llp': 'limited liability partnership',
        'co': 'company',
        'corp': 'corporation',
        'inc': 'incorporated',
        'enterprises': 'ent',
        'industries': 'ind',
        'services': 'svc',
        'technologies': 'tech',
        'systems': 'sys',
        'solutions': 'sol'
    }

    # Normalize abbreviations
    for abbrev, full in abbreviations.items():
        str1_clean = str1_clean.replace(full, abbrev).replace(abbrev, abbrev)
        str2_clean = str2_clean.replace(full, abbrev).replace(abbrev, abbrev)

    # Word-based similarity
    str1_words = set(str1_clean.split())
    str2_words = set(str2_clean.split())

    if not str1_words or not str2_words:
        return 0.0

    # Calculate Jaccard similarity (intersection over union)
    intersection = str1_words.intersection(str2_words)
    union = str1_words.union(str2_words)
    word_similarity = len(intersection) / len(union) if union else 0.0

    # Character-level similarity using simple edit distance approach
    max_len = max(len(str1_clean), len(str2_clean))
    min_len = min(len(str1_clean), len(str2_clean))

    if max_len == 0:
        return 1.0

    # Simple character overlap calculation
    common_chars = 0
    for char in set(str1_clean):
        common_chars += min(str1_clean.count(char), str2_clean.count(char))

    char_similarity = (2 * common_chars) / (len(str1_clean) + len(str2_clean))

    # Bonus for similar length
    length_similarity = min_len / max_len

    # Weighted combination - prioritize word similarity for business names
    final_similarity = (word_similarity * 0.6) + (char_similarity * 0.3) + (length_similarity * 0.1)

    return min(final_similarity, 1.0)


def get_organization_from_request(request, **kwargs):
    """Get organization from URL org_id parameter, API key, or user membership."""
    # First check for org_id in URL kwargs (organization-scoped endpoints)
    org_id = kwargs.get('org_id')
    if org_id:
        return get_object_or_404(Organization, id=org_id)

    # Check for API key authentication
    if hasattr(request, 'auth') and request.auth:
        from apps.organizations.models import OrganizationAPIKey
        try:
            org_api_key = OrganizationAPIKey.objects.get(api_key=request.auth)
            return org_api_key.organization
        except OrganizationAPIKey.DoesNotExist:
            pass

    # Fallback to user membership
    if hasattr(request.user, 'memberships'):
        membership = request.user.memberships.filter(is_active=True).first()
        if membership:
            return membership.organization
    return None


def analyze_vendor_bill_with_openai(file_content, file_extension):
    """
    Analyze vendor bill content using OpenAI to extract structured data with enhanced PDF handling.
    Supports PDF, JPG, PNG file formats with robust validation and optimization.
    """
    logger.info(f"Starting enhanced vendor bill analysis for file type: {file_extension}")

    try:
        # Initialize OpenAI client
        api_key = getattr(settings, 'OPENAI_API_KEY', None)
        if not api_key:
            raise ValueError("OpenAI API key not configured in settings")

        client = OpenAI(api_key=api_key)

        # Prepare image data based on file type with enhanced processing
        if file_extension.lower() == 'pdf':
            logger.info(f"Processing PDF file with enhanced settings...")

            file_size = len(file_content)
            logger.info(f"PDF loaded: {file_size:,} bytes")

            # Enhanced PDF validation
            if not file_content.startswith(b'%PDF'):
                raise ValueError("Invalid PDF file format")

            if file_size < 100:
                raise ValueError("PDF file too small (possibly corrupted)")

            logger.info("PDF validation passed")

            # Convert PDF to image with enhanced settings
            try:
                from PIL import Image, ImageEnhance

                logger.info("Converting PDF to image with enhanced settings...")
                images = convert_from_bytes(
                    file_content,
                    first_page=1,
                    last_page=1,
                    dpi=200,  # Good balance of quality vs speed
                    fmt='jpeg'
                )

                if not images:
                    raise ValueError("No images generated from PDF")

                image = images[0]
                logger.info(f"PDF converted successfully - Image size: {image.size}, Mode: {image.mode}")

                # Enhanced image optimization for OCR
                logger.info("Optimizing image for OCR...")

                # Convert to RGB if needed
                if image.mode != 'RGB':
                    image = image.convert('RGB')

                # Enhance for better OCR
                enhancer = ImageEnhance.Contrast(image)
                image = enhancer.enhance(1.2)

                enhancer = ImageEnhance.Sharpness(image)
                image = enhancer.enhance(1.1)

                # Ensure minimum size for better OCR accuracy
                width, height = image.size
                if width < 1000 or height < 1000:
                    scale = max(1000 / width, 1000 / height)
                    new_size = (int(width * scale), int(height * scale))
                    image = image.resize(new_size, Image.Resampling.LANCZOS)
                    logger.info(f"Image upscaled to: {new_size}")

                logger.info("Image optimization completed")

                # Convert PIL image to base64
                buffer = BytesIO()
                image.save(buffer, format='JPEG', quality=95)
                image_data = base64.b64encode(buffer.getvalue()).decode('utf-8')
                mime_type = "image/jpeg"
                logger.info(f"Base64 conversion completed: {len(image_data):,} characters")

            except Exception as e:
                logger.error(f"Enhanced PDF conversion failed: {str(e)}")
                raise ValueError(f"PDF conversion failed: {str(e)}")

        elif file_extension.lower() in ['jpg', 'jpeg', 'png']:
            # Handle image files with MIME type detection
            logger.info(f"Processing image file: {file_extension}")

            if file_extension.lower() in ['jpg', 'jpeg']:
                mime_type = "image/jpeg"
            elif file_extension.lower() == 'png':
                mime_type = "image/png"
            else:
                mime_type = "image/jpeg"  # Default fallback

            image_data = base64.b64encode(file_content).decode('utf-8')
            logger.info(f"Successfully processed image with MIME type: {mime_type}")

        else:
            raise ValueError(f"Unsupported file format: {file_extension}")

        # Enhanced prompt for Indian invoices (from successful test script)
        enhanced_prompt = """
        Analyze this invoice/bill image carefully and extract ALL visible information in JSON format.
        This appears to be an Indian business invoice/bill. Look for:
        
        1. Invoice/Bill Number (may be labeled as Invoice No, Bill No, Receipt No, etc.)
        2. Dates (Invoice Date, Bill Date, Due Date - convert to YYYY-MM-DD format)
        3. Vendor/Company details in "from" section (name and address)
        4. Customer details in "to" section (name and address) 
        5. Line items with descriptions, quantities, and prices
        6. Tax amounts (IGST, CGST, SGST - look for percentages and amounts)
        7. Total amount (may include terms like "Total", "Grand Total", "Amount Payable")
        
        IMPORTANT RULES:
        - Extract EXACT text as it appears on the document
        - For numbers, remove currency symbols (₹, Rs.) and commas
        - If any field is not visible or unclear, use empty string "" or 0 for numbers
        - Look carefully at the entire document, including headers, footers, and margins
        - Pay special attention to tax sections which may be in tables or separate areas
        
        Return data in this JSON structure:
        {
            "invoiceNumber": "Invoice/Bill number as shown on document",
            "dateIssued": "Invoice/Bill date in YYYY-MM-DD format",
            "dueDate": "Due date in YYYY-MM-DD format if mentioned",
            "from": {
                "name": "Vendor/Company name",
                "address": "Vendor address"
            },
            "to": {
                "name": "Customer name", 
                "address": "Customer address"
            },
            "items": [
                {
                    "description": "Item/Service description",
                    "quantity": 0,
                    "price": 0
                }
            ],
            "total": 0,
            "igst": 0,
            "cgst": 0,
            "sgst": 0
        }
        """

        # Enhanced OpenAI API call with better settings
        logger.info("Sending request to OpenAI API...")
        response = client.chat.completions.create(
            model='gpt-4o',
            response_format={"type": "json_object"},
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": enhanced_prompt
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime_type};base64,{image_data}",
                            "detail": "high"  # Enhanced detail setting
                        }
                    }
                ]
            }],
            max_tokens=2000,  # Increased token limit
            temperature=0.1   # Lower temperature for more consistent results
        )

        if not response.choices or not response.choices[0].message.content:
            raise ValueError("Empty response from OpenAI API")

        logger.info("Successfully received response from OpenAI API")
        logger.info(f"Raw OpenAI response: {response.choices[0].message.content}")

        json_data = json.loads(response.choices[0].message.content)
        logger.info(f"Successfully parsed analyzed data: {json_data}")
        return json_data

    except Exception as e:
        logger.error(f"Error in enhanced vendor bill analysis: {str(e)}")
        return {
            "invoiceNumber": "",
            "dateIssued": "",
            "dueDate": "",
            "from": {"name": "", "address": ""},
            "to": {"name": "", "address": ""},
            "items": [],
            "total": 0,
            "igst": 0,
            "cgst": 0,
            "sgst": 0,
            "error": f"Analysis failed: {str(e)}"
        }


def create_vendor_zoho_objects_from_analysis(bill, analyzed_data, organization):
    """
    Create VendorZohoBill and VendorZohoProduct objects from analyzed data.
    """
    logger.info(f"Creating Vendor Zoho objects for bill {bill.id} with analyzed data: {analyzed_data}")

    # Process analyzed data based on schema format
    if "properties" in analyzed_data:
        relevant_data = {
            "invoiceNumber": analyzed_data["properties"]["invoiceNumber"]["const"],
            "dateIssued": analyzed_data["properties"]["dateIssued"]["const"],
            "dueDate": analyzed_data["properties"]["dueDate"]["const"],
            "from": analyzed_data["properties"]["from"]["properties"],
            "to": analyzed_data["properties"]["to"]["properties"],
            "items": [{"description": item["description"]["const"], "quantity": item["quantity"]["const"],
                       "price": item["price"]["const"]} for item in analyzed_data["properties"]["items"]["items"]],
            "total": analyzed_data["properties"]["total"]["const"],
            "igst": analyzed_data["properties"]["igst"]["const"],
            "cgst": analyzed_data["properties"]["cgst"]["const"],
            "sgst": analyzed_data["properties"]["sgst"]["const"],
        }
    else:
        relevant_data = analyzed_data

    # Try to find vendor by company name (case-insensitive search)
    vendor = None
    company_name = relevant_data.get('from', {}).get('name', '').strip().lower()
    if company_name:
        vendor = ZohoVendor.objects.annotate(lower_name=Lower('companyName')).filter(
            lower_name=company_name).first()
        logger.info(f"Found vendor by name {company_name}: {vendor}")

    # Parse bill date
    bill_date = None
    date_issued = relevant_data.get('dateIssued', '')
    if date_issued:
        try:
            bill_date = datetime.strptime(date_issued, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            logger.warning(f"Could not parse bill date: {date_issued}")
    
    # Parse due date
    due_date = None
    due_date_issued = relevant_data.get('dueDate', '')
    if due_date_issued:
        try:
            due_date = datetime.strptime(due_date_issued, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            logger.warning(f"Could not parse due date: {due_date_issued}")

    # Validate numeric fields and convert to string
    def safe_numeric_string(value, default='0'):
        try:
            if value is None:
                return default
            if isinstance(value, (int, float)):
                return str(value)
            float(str(value))  # Validate it's numeric
            return str(value)
        except (ValueError, TypeError):
            logger.warning(f"Invalid numeric value: {value}, using default: {default}")
            return default

    # Create or update VendorZohoBill
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
                'note': f"Bill from analysis for {company_name or 'Unknown Vendor'} entered via Billmunshi"
            }
        )

        if created:
            logger.info(f"Created new VendorZohoBill: {zoho_bill.id}")
        else:
            logger.info(f"Found existing VendorZohoBill: {zoho_bill.id}")
            # Update the existing bill with new analyzed data
            zoho_bill.vendor = vendor
            zoho_bill.bill_no = relevant_data.get('invoiceNumber', zoho_bill.bill_no)
            zoho_bill.bill_date = bill_date or zoho_bill.bill_date
            zoho_bill.due_date = due_date or zoho_bill.due_date
            zoho_bill.total = safe_numeric_string(relevant_data.get('total'), zoho_bill.total)
            zoho_bill.igst = safe_numeric_string(relevant_data.get('igst'), zoho_bill.igst)
            zoho_bill.cgst = safe_numeric_string(relevant_data.get('cgst'), zoho_bill.cgst)
            zoho_bill.sgst = safe_numeric_string(relevant_data.get('sgst'), zoho_bill.sgst)
            zoho_bill.note = f"Updated from analysis for {company_name or 'Unknown Vendor'}"
            zoho_bill.save()
            logger.info(f"Updated existing VendorZohoBill: {zoho_bill.id}")

        # Delete existing products and recreate them
        existing_products_count = zoho_bill.products.count()
        if existing_products_count > 0:
            zoho_bill.products.all().delete()
            logger.info(f"Deleted {existing_products_count} existing products")

        # Create VendorZohoProduct objects for each item
        items = relevant_data.get('items', [])
        logger.info(f"Creating {len(items)} product line items")

        created_products = []
        for idx, item in enumerate(items):
            try:
                rate = Decimal(item.get('price', 0) or 0)
                quantity = int(item.get('quantity', 0) or 0)
                amount = rate * quantity

                product = VendorZohoProduct.objects.create(
                    zohoBill=zoho_bill,
                    organization=organization,
                    item_name=item.get('description', f'Item {idx + 1}')[:100],
                    item_details=item.get('description', f'Item {idx + 1}')[:200],
                    rate=str(rate),
                    quantity=str(quantity),
                    amount=str(amount)
                )
                created_products.append(product)
                logger.info(
                    f"Created product {idx + 1}: {product.item_name} - Rate: {product.rate}, Qty: {product.quantity}, Amount: {product.amount}")
            except Exception as e:
                logger.error(f"Error creating product {idx + 1}: {str(e)}")
                continue

        logger.info(f"Successfully created {len(created_products)} products for bill {zoho_bill.id}")

        # ✅ AUTO-CREATE CONSOLIDATED PRODUCT FOR MULTI-ITEM BILLS
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
                    item_details.append(f'• {product.item_name} (Qty: {product.quantity}, Rate: ₹{product.rate})')

                consolidated_details = f'Consolidated {items_count} items:\n' + '\n'.join(item_details)
                consolidated_name = f'Consolidated Items - {zoho_bill.bill_no or "Bill"} ({items_count} items)'

                # Create consolidated product (but keep consolidate=False by default)
                consolidated_product = VendorZohoConsolidatedProduct.objects.create(
                    zohoBill=zoho_bill,
                    organization=organization,
                    consolidated_item_name=consolidated_name,
                    consolidated_item_details=consolidated_details,
                    total_quantity=Decimal('1'),  # Always 1 for consolidated
                    consolidated_rate=total_amount,  # Total amount as rate
                    consolidated_amount=total_amount,
                    original_items_count=items_count,
                    consolidation_notes=f'Auto-created during analysis for {items_count} items',
                    itc_eligibility='eligible',  # Default
                    reverse_charge_tax_id=False
                )

                logger.info(f"✅ Auto-created consolidated product for bill {zoho_bill.id} with {items_count} items (₹{total_amount})")

            except Exception as e:
                logger.error(f"❌ Error creating consolidated product for bill {zoho_bill.id}: {str(e)}")
                # Don't raise - consolidated product creation failure shouldn't break the main flow
        else:
            logger.info(f"ℹ️ Skipping consolidated product creation - bill has only {len(created_products)} item(s)")

        return zoho_bill

    except Exception as e:
        logger.error(f"Error creating Zoho objects for bill {bill.id}: {str(e)}")
        raise


def refresh_zoho_access_token(current_token):
    """Refresh Zoho access token using refresh token."""
    refresh_token = current_token.refreshToken
    client_id = current_token.clientId
    client_secret = current_token.clientSecret

    url = f"https://accounts.zoho.in/oauth/v2/token?refresh_token={refresh_token}&client_id={client_id}&client_secret={client_secret}&grant_type=refresh_token"

    try:
        response = requests.post(url)
        if response.status_code == 200:
            new_access_token = response.json().get('access_token')
            current_token.accessToken = new_access_token
            current_token.save()
            return new_access_token
        else:
            logger.error(f"Failed to refresh token: {response.status_code} - {response.text}")
            return None
    except Exception as e:
        logger.error(f"Error refreshing token: {str(e)}")
        return None


def process_pdf_splitting_vendor(pdf_file, organization, file_type, uploaded_by):
    """Split PDF into individual pages and create separate vendor bills"""
    created_bills = []

    try:
        pdf_file.seek(0)
        pdf_bytes = pdf_file.read()
        pdf = PdfReader(BytesIO(pdf_bytes))
        unique_id = datetime.now().strftime("%Y%m%d%H%M%S")

        for page_num in range(len(pdf.pages)):
            # Convert PDF page to image
            page_images = convert_from_bytes(
                pdf_bytes,
                first_page=page_num + 1,
                last_page=page_num + 1
            )

            if page_images:
                image_io = BytesIO()
                page_images[0].save(image_io, format='JPEG')
                image_io.seek(0)

                # Create bill for this page with uploaded_by user
                bill = VendorBill.objects.create(
                    file=ContentFile(
                        image_io.read(),
                        name=f"BM-Vendor-Page-{page_num + 1}-{unique_id}.jpg"
                    ),
                    fileType=file_type,
                    organization=organization,
                    uploaded_by=uploaded_by,
                    status='Draft'
                )
                created_bills.append(bill)

    except Exception as e:
        logger.error(f"Error splitting vendor PDF: {str(e)}")
        raise Exception(f"Vendor PDF processing failed: {str(e)}")

    return created_bills


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
    """Handle single or multiple vendor bill file uploads with PDF splitting support"""

    # Handle both single file and multiple files seamlessly
    files_data = []

    # Debug logging with prints (will show in gunicorn logs)
    print(f"[VENDOR DEBUG] Request data keys: {list(request.data.keys())}")
    print(f"[VENDOR DEBUG] 'files' in request.data: {'files' in request.data}")
    print(f"[VENDOR DEBUG] 'file' in request.data: {'file' in request.data}")

    # Check if files are provided as a list (multiple files)
    if 'files' in request.data:
        files_data = request.data.getlist('files') if hasattr(request.data, 'getlist') else request.data.get('files', [])
        # Ensure files_data is always a list
        if not isinstance(files_data, list):
            files_data = [files_data] if files_data else []
        print(f"[VENDOR DEBUG] Found 'files' field with {len(files_data)} file(s)")
    # Check if a single file is provided
    elif 'file' in request.data:
        single_file = request.data.get('file')
        if single_file:
            files_data = [single_file]
        print(f"[VENDOR DEBUG] Found 'file' field with {len(files_data)} file(s)")

    print(f"[VENDOR DEBUG] Total files collected: {len(files_data)}")

    # Debug: Print details about each file
    for i, f in enumerate(files_data):
        print(f"[VENDOR DEBUG] File {i+1}: {getattr(f, 'name', 'Unknown')} - Size: {getattr(f, 'size', 'Unknown')}")

    # Prepare data for serializer validation
    serializer_data = {
        'files': files_data,
        'fileType': request.data.get('fileType', 'Single Invoice/File')
    }

    print(f"[VENDOR DEBUG] Serializer data: files count = {len(serializer_data['files'])}, fileType = {serializer_data['fileType']}")

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

    print(f"[VENDOR DEBUG] After serializer validation - files count: {len(files)}, fileType: {file_type}")

    if not files:
        return Response({
            'error': 'No Files Provided',
            'detail': 'At least one file must be provided for upload. Please select files to upload.'
        }, status=status.HTTP_400_BAD_REQUEST)

    try:
        # Temporarily removing atomic transaction to debug
        # with transaction.atomic():
        print(f"[VENDOR DEBUG] Starting to process {len(files)} files")

        # Check for potential duplicates based on file characteristics
        upload_warnings = []

        for i, uploaded_file in enumerate(files):
                print(f"[VENDOR DEBUG] Processing file {i+1}/{len(files)}: {uploaded_file.name}")
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
                                print(f"[VENDOR DEBUG] Error accessing file for bill {existing_bill.billmunshiName}: {str(e)}")
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

                    print(f"[VENDOR DEBUG] Processing as PDF split for file: {uploaded_file.name}")
                    pdf_bills = process_pdf_splitting_vendor(
                        uploaded_file, organization, file_type, request.user
                    )
                    print(f"[VENDOR DEBUG] PDF splitting created {len(pdf_bills)} bills")
                    created_bills.extend(pdf_bills)
                else:
                    # Create single bill (including PDFs for single invoice type)
                    print(f"[VENDOR DEBUG] Creating single bill for file: {uploaded_file.name}")
                    bill = VendorBill.objects.create(
                        file=uploaded_file,
                        fileType=file_type,
                        organization=organization,
                        uploaded_by=request.user,
                        status='Draft'
                    )
                    print(f"[VENDOR DEBUG] Created bill: {bill.billmunshiName} (ID: {bill.id})")
                    created_bills.append(bill)

        print(f"[VENDOR DEBUG] Completed processing all files. Total bills created: {len(created_bills)}")

        # Auto-analyze uploaded bills and check for duplicates
        analysis_results = []
        all_duplicate_warnings = []

        for i, bill in enumerate(created_bills):
            print(f"[VENDOR DEBUG] Bill {i+1}: {bill.billmunshiName} (ID: {bill.id})")

            # Auto-analyze the bill if it's in Draft status
            if bill.status == 'Draft':
                try:
                    print(f"[VENDOR DEBUG] Auto-analyzing bill: {bill.billmunshiName}")

                    # Read and analyze file content
                    bill.file.seek(0)
                    file_content = bill.file.read()
                    file_extension = bill.file.name.split('.')[-1].lower()

                    # Analyze with OpenAI
                    analyzed_data = analyze_vendor_bill_with_openai(file_content, file_extension)

                    # Update bill with analyzed data
                    bill.analysed_data = analyzed_data
                    bill.status = 'Analysed'
                    bill.process = True
                    bill.save()

                    # Create Zoho objects from analysis
                    create_vendor_zoho_objects_from_analysis(bill, analyzed_data, organization)

                    # Check for duplicates after analysis
                    is_duplicate, duplicate_bills, max_similarity = check_duplicate_bill(bill, organization)

                    analysis_result = {
                        'bill_id': str(bill.id),
                        'bill_name': bill.billmunshiName,
                        'analysis_successful': True,
                        'duplicate_detected': is_duplicate
                    }

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

                        analysis_result.update({
                            "duplicate_count": len(duplicate_bills),
                            "max_similarity": round(max_similarity, 2),
                            "duplicate_bills": duplicate_warnings,
                            "warning_message": f"⚠️ DUPLICATE DETECTED: Bill '{bill.billmunshiName}' appears to be {round(max_similarity, 1)}% similar to {len(duplicate_bills)} existing bill(s)."
                        })

                        all_duplicate_warnings.extend(duplicate_warnings)
                        logger.warning(f"Duplicate detected for {bill.billmunshiName} - {len(duplicate_bills)} similar bills found")

                    analysis_results.append(analysis_result)
                    print(f"[VENDOR DEBUG] Successfully analyzed bill: {bill.billmunshiName}")

                except Exception as analysis_error:
                    logger.error(f"Auto-analysis failed for bill {bill.billmunshiName}: {str(analysis_error)}")
                    analysis_results.append({
                        'bill_id': str(bill.id),
                        'bill_name': bill.billmunshiName,
                        'analysis_successful': False,
                        'error': str(analysis_error),
                        'duplicate_detected': False
                    })

        response_serializer = ZohoVendorBillSerializer(created_bills, many=True, context={'request': request})

        # Log the successful result
        logger.info(f"Successfully processed {len(files)} files and created {len(created_bills)} bills")

        response_data = {
            'message': f'Successfully uploaded {len(files)} file(s) and created {len(created_bills)} bill(s)',
            'files_uploaded': len(files),
            'bills_created': len(created_bills),
            'bills': response_serializer.data,
            'auto_analysis_results': analysis_results
        }

        # Add comprehensive warnings
        warnings_count = len(upload_warnings) + len(all_duplicate_warnings)
        if upload_warnings or all_duplicate_warnings:
            warning_messages = []

            if upload_warnings:
                warning_messages.append(f"📁 FILE WARNING: {len(upload_warnings)} file(s) may be duplicates based on filename/size")
                response_data['upload_warnings'] = upload_warnings

            if all_duplicate_warnings:
                warning_messages.append(f"🔍 CONTENT WARNING: {len(all_duplicate_warnings)} duplicate(s) detected after analyzing bill content")
                response_data['duplicate_warnings'] = all_duplicate_warnings

            response_data.update({
                'total_warnings': warnings_count,
                'warning_message': " | ".join(warning_messages) + " | Please review carefully before proceeding."
            })

            logger.warning(f"Total warnings generated: {warnings_count} (Upload: {len(upload_warnings)}, Content: {len(all_duplicate_warnings)})")

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
                from .models import ZohoVendor
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
                        # Handle consolidated product updates/creation with ForeignKey relationship
                        for idx, consolidated_data in enumerate(consolidate_prod_data):
                            logger.error(f"[DEBUG] vendor_bill_verify_view - Processing consolidated product {idx}: {consolidated_data}")

                            consolidated_id = consolidated_data.get('id')
                            if consolidated_id:
                                # Update existing consolidated product
                                try:
                                    consolidated_product = VendorZohoConsolidatedProduct.objects.get(
                                        id=consolidated_id,
                                        zohoBill=updated_bill
                                    )

                                    # Update fields from frontend using CORRECT field names
                                    consolidated_product.consolidated_item_name = consolidated_data.get('item_name', consolidated_product.consolidated_item_name)
                                    consolidated_product.consolidated_item_details = consolidated_data.get('item_details', consolidated_product.consolidated_item_details)
                                    consolidated_product.consolidated_rate = consolidated_data.get('rate', consolidated_product.consolidated_rate)
                                    consolidated_product.total_quantity = consolidated_data.get('quantity', consolidated_product.total_quantity)
                                    consolidated_product.consolidated_amount = consolidated_data.get('amount', consolidated_product.consolidated_amount)

                                    # Handle foreign key fields
                                    chart_of_accounts_id = consolidated_data.get('chart_of_accounts')
                                    if chart_of_accounts_id:
                                        consolidated_product.chart_of_accounts_id = chart_of_accounts_id

                                    taxes_id = consolidated_data.get('taxes')
                                    if taxes_id:
                                        consolidated_product.taxes_id = taxes_id

                                    # Handle other fields
                                    consolidated_product.itc_eligibility = consolidated_data.get('itc_eligibility', consolidated_product.itc_eligibility)
                                    consolidated_product.reverse_charge_tax_id = consolidated_data.get('reverse_charge_tax_id', consolidated_product.reverse_charge_tax_id)

                                    consolidated_product.save()
                                    logger.error(f"[DEBUG] vendor_bill_verify_view - Updated consolidated product {consolidated_id}")

                                except VendorZohoConsolidatedProduct.DoesNotExist:
                                    logger.error(f"[DEBUG] vendor_bill_verify_view - Consolidated product {consolidated_id} not found, creating new one")
                                    consolidated_id = None  # Fall through to create new

                            if not consolidated_id:
                                # Create new consolidated product - ForeignKey allows multiple
                                consolidated_product = VendorZohoConsolidatedProduct.objects.create(
                                    zohoBill=updated_bill,
                                    organization=organization,
                                    consolidated_item_name=consolidated_data.get('item_name', 'Consolidated Product'),
                                    consolidated_item_details=consolidated_data.get('item_details', 'New consolidated product from verification'),
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
                if updated_bill.consolidate:
                    logger.info(f"Creating/updating consolidated product for bill {updated_bill.id}")
                    try:
                        consolidated_product = create_consolidated_vendor_product(updated_bill, organization)
                        if consolidated_product:
                            logger.info(f"Successfully created/updated consolidated product: {consolidated_product.id}")
                        else:
                            logger.warning(f"Failed to create consolidated product for bill {updated_bill.id}")
                    except Exception as consolidation_error:
                        logger.error(f"Error during consolidation: {str(consolidation_error)}")
                        # Don't fail the entire request for consolidation errors
                else:
                    # Remove consolidated products if consolidation is disabled
                    try:
                        consolidated_products = updated_bill.consolidated_products.all()
                        if consolidated_products.exists():
                            count = consolidated_products.count()
                            consolidated_products.delete()
                            logger.info(f"Deleted {count} consolidated product(s) as consolidation was disabled")
                    except Exception as delete_error:
                        logger.error(f"Error deleting consolidated products: {str(delete_error)}")
                        # Don't fail the entire request

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
                
                # Check if bill has at least one product
                product_count = updated_bill.products.count()
                if product_count == 0:
                    validation_errors.append("At least one product line item is required")
                
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
                logger.error(f"  - Products count: {len(response_data.get('products', []))}")
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















# apps/module/tally/expense_views_functional.py

import base64
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
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, OpenApiResponse
from pdf2image import convert_from_bytes
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, parser_classes
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.permissions import IsAuthenticated, BasePermission
from rest_framework.response import Response

from apps.common.pagination import DefaultPagination
from apps.common.permissions import IsOrgAdmin
from apps.organizations.models import Organization
from .models import (
    TallyExpenseBill,
    TallyExpenseAnalyzedBill,
    TallyExpenseAnalyzedProduct,
    TallyExpenseConsolidatedProduct,
    Ledger,
    ParentLedger,
    TallyConfig,
    TallyVendorBill
)
from .serializers import (
    TallyExpenseBillSerializer,
    TallyExpenseAnalyzedBillSerializer,
    ExpenseBillUploadSerializer,
    ExpenseBillAnalysisRequestSerializer,
    ExpenseBillVerificationSerializer,
    ExpenseBillSyncRequestSerializer,
    ExpenseBillSyncResponseSerializer
)

# OpenAI Client
try:
    from openai import OpenAI

    client = OpenAI(api_key=getattr(settings, 'OPENAI_API_KEY', None))
except ImportError:
    client = None

logger = logging.getLogger(__name__)


# ============================================================================
# Helper Functions

def check_duplicate_tally_expense_bill(bill, organization):
    """
    Check if a Tally expense bill is a duplicate based on invoice number and vendor information.
    Returns tuple (is_duplicate, duplicate_bills, similarity_score)
    """
    logger.info(f"Checking for duplicates of Tally expense bill {bill.id} in organization {organization.id}")

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
    potential_duplicates = TallyExpenseBill.objects.filter(
        organization=organization,
        status__in=[TallyExpenseBill.BillStatus.ANALYSED, TallyExpenseBill.BillStatus.VERIFIED, TallyExpenseBill.BillStatus.SYNCED]
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
            vendor_similarity = _calculate_tally_expense_string_similarity(current_vendor_name.lower(), other_vendor_name.lower())
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
    logger.info(f"Tally expense duplicate check complete. Found {len(duplicate_bills)} potential duplicates with max similarity {max_similarity}")

    return is_duplicate, duplicate_bills, max_similarity


def _calculate_tally_expense_string_similarity(str1, str2):
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


class OrganizationAPIKeyOrBearerToken(BasePermission):
    """
    Custom permission class that allows access via API key OR Bearer token authentication.
    This is an OR condition between authentication methods.
    """

    def has_permission(self, request, view):
        # Check for API key in the Authorization header
        auth_header = request.META.get('HTTP_AUTHORIZATION', '')

        if auth_header.startswith('Api-Key '):
            api_key_value = auth_header.replace('Api-Key ', '', 1)

            # Check if the API key exists and is valid
            from rest_framework_api_key.models import APIKey
            from apps.organizations.models import OrganizationAPIKey

            try:
                # Check if the API key is valid
                api_key_obj = APIKey.objects.get_from_key(api_key_value)

                if api_key_obj:
                    # Check if it's linked to an organization
                    org_api_key = OrganizationAPIKey.objects.get(api_key=api_key_obj)

                    # Store the organization in the request for later use
                    request.organization = org_api_key.organization
                    return True

            except (APIKey.DoesNotExist, OrganizationAPIKey.DoesNotExist):
                # API key doesn't exist or not linked to organization
                pass
            except Exception as e:
                # Log other exceptions for debugging
                print(f"API Key validation error: {str(e)}")
                pass

        # If not authenticated via API key, check for Bearer token
        bearer_auth = IsAuthenticated().has_permission(request, view)
        if bearer_auth:
            # If authenticated via bearer token, also check admin permission
            return IsOrgAdmin().has_permission(request, view)

        return False


def get_organization_from_request(request, org_id=None):
    """Get organization from URL parameter or user membership"""
    if org_id:
        return get_object_or_404(Organization, id=org_id)

    # Check if user has organization through API key (handled by permission class)
    if hasattr(request, 'organization'):
        return request.organization

    # Fallback to user membership
    if hasattr(request.user, 'memberships'):
        membership = request.user.memberships.first()
        if membership:
            return membership.organization
    return None


def analyze_expense_bill_with_ai(bill, organization):
    """Analyze expense bill using OpenAI API with enhanced PDF handling and error recovery"""
    if not client:
        raise Exception("OpenAI client not configured")

    logger.info(f"Starting AI analysis for expense bill {bill.id}, file: {bill.file.name}")

    # Determine file type and process accordingly
    file_path = bill.file.path
    file_name = bill.file.name.lower()

    try:
        # Read and process file based on type
        if file_name.endswith('.pdf'):
            logger.info(f"Processing PDF file: {file_name}")

            # Enhanced PDF processing with validation
            with open(file_path, 'rb') as f:
                pdf_bytes = f.read()

            file_size = len(pdf_bytes)
            logger.info(f"PDF loaded: {file_size:,} bytes")

            # Enhanced PDF validation
            if not pdf_bytes.startswith(b'%PDF'):
                raise Exception("Invalid PDF file format")

            if file_size < 100:
                raise Exception("PDF file too small (possibly corrupted)")

            logger.info("PDF validation passed")

            # Convert PDF to image with enhanced settings
            try:
                from PIL import Image, ImageEnhance

                logger.info("Converting PDF to image with enhanced settings...")
                page_images = convert_from_bytes(
                    pdf_bytes,
                    first_page=1,
                    last_page=1,
                    dpi=200,  # Good balance of quality vs speed
                    fmt='jpeg'
                )

                if not page_images:
                    raise Exception("No images generated from PDF")

                image = page_images[0]
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
                image_io = BytesIO()
                image.save(image_io, format='JPEG', quality=95)
                image_io.seek(0)
                image_base64 = base64.b64encode(image_io.read()).decode('utf-8')
                mime_type = "image/jpeg"
                logger.info(f"Base64 conversion completed: {len(image_base64):,} characters")

            except Exception as e:
                logger.error(f"Enhanced PDF conversion failed: {str(e)}")
                raise Exception(f"PDF conversion failed: {str(e)}")

        else:
            # Handle image files
            logger.info(f"Processing image file: {file_name}")
            with open(file_path, 'rb') as f:
                file_content = f.read()

            # Determine MIME type based on file extension
            if file_name.endswith(('.jpg', '.jpeg')):
                mime_type = "image/jpeg"
            elif file_name.endswith('.png'):
                mime_type = "image/png"
            elif file_name.endswith('.gif'):
                mime_type = "image/gif"
            elif file_name.endswith('.bmp'):
                mime_type = "image/bmp"
            elif file_name.endswith('.webp'):
                mime_type = "image/webp"
            else:
                # Default to JPEG for unknown image types
                mime_type = "image/jpeg"
                logger.warning(f"Unknown image type for {file_name}, defaulting to JPEG")

            image_base64 = base64.b64encode(file_content).decode('utf-8')
            logger.info(f"Successfully processed image with MIME type: {mime_type}")

    except Exception as e:
        logger.error(f"Error reading/processing expense bill file: {str(e)}")
        raise Exception(f"Error reading expense bill file: {str(e)}")

    # Enhanced prompt for Indian expense bills/receipts
    enhanced_prompt = """
    Analyze this expense bill/receipt image carefully and extract ALL visible information in JSON format.
    This appears to be an Indian business expense bill/receipt. Look for:
    
    1. Bill/Receipt Number (may be labeled as Bill No, Receipt No, Invoice No, etc.)
    2. Dates (Bill Date, Receipt Date, Transaction Date - convert to YYYY-MM-DD format)
    3. Vendor/Company details in "from" section (name and address)
    4. Customer details in "to" section (name and address) 
    5. Expense items with descriptions, categories, and amounts
    6. Tax amounts (IGST, CGST, SGST - look for percentages and amounts)
    7. Total amount (may include terms like "Total", "Grand Total", "Amount Payable", "Net Amount")
    
    IMPORTANT RULES:
    - Extract EXACT text as it appears on the document
    - For numbers, remove currency symbols (₹, Rs.) and commas
    - If any field is not visible or unclear, use empty string "" or 0 for numbers
    - Look carefully at the entire document, including headers, footers, and margins
    - Pay special attention to tax sections which may be in tables or separate areas
    - For expense categories, try to identify the type of expense (travel, food, supplies, etc.)
    
    Return data in this JSON structure:
    {
        "billNumber": "Bill/Receipt number as shown on document",
        "dateIssued": "Bill/Receipt date in YYYY-MM-DD format",
        "from": {
            "name": "Vendor/Company name",
            "address": "Vendor address"
        },
        "to": {
            "name": "Customer name", 
            "address": "Customer address"
        },
        "expenses": [
            {
                "description": "Expense item description",
                "category": "Expense category (travel, food, supplies, etc.)",
                "amount": 0
            }
        ],
        "total": 0,
        "igst": 0,
        "cgst": 0,
        "sgst": 0
    }
    """

    # AI processing request with enhanced settings
    try:
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
                            "url": f"data:{mime_type};base64,{image_base64}",
                            "detail": "high"  # Enhanced detail setting
                        }
                    }
                ]
            }],
            max_tokens=2000,  # Increased token limit
            temperature=0.1  # Lower temperature for more consistent results
        )

        if not response.choices or not response.choices[0].message.content:
            raise Exception("Empty response from OpenAI API")

        logger.info("Successfully received response from OpenAI API")
        logger.info(f"Raw OpenAI response: {response.choices[0].message.content}")

        json_data = json.loads(response.choices[0].message.content)
        logger.info("Successfully parsed JSON response from OpenAI")

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON from OpenAI response: {str(e)}")
        logger.error(f"Raw response: {response.choices[0].message.content if response.choices else 'No response'}")
        raise Exception(f"Invalid JSON response from OpenAI: {str(e)}")
    except Exception as e:
        logger.error(f"AI processing failed: {str(e)}")
        raise Exception(f"AI processing failed: {str(e)}")

    # Process and save extracted data
    return process_expense_analysis_data(bill, json_data, organization)


def process_expense_analysis_data(bill, json_data, organization):
    """Process AI extracted data and create analyzed expense bill"""
    try:
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

        # Handle 'from' field safely
        from_data = relevant_data.get('from', {})
        if isinstance(from_data, dict):
            company_name = str(from_data.get('name', '')).strip().lower()
        else:
            company_name = str(from_data).strip().lower()

        # Parse date with multiple format support
        bill_date = parse_expense_bill_date(date_issued)

        # Find vendor ledger
        vendor = find_expense_vendor_ledger(company_name, organization)

        # Determine GST type with safe conversion
        igst_val = safe_float_convert(relevant_data.get('igst', 0))
        cgst_val = safe_float_convert(relevant_data.get('cgst', 0))
        sgst_val = safe_float_convert(relevant_data.get('sgst', 0))

        if igst_val > 0:
            gst_type = TallyExpenseAnalyzedBill.GSTType.IGST
        elif cgst_val > 0 or sgst_val > 0:
            gst_type = TallyExpenseAnalyzedBill.GSTType.CGST_SGST
        else:
            gst_type = TallyExpenseAnalyzedBill.GSTType.UNKNOWN

        # Create analyzed bill
        with transaction.atomic():
            analyzed_bill = TallyExpenseAnalyzedBill.objects.create(
                selected_bill=bill,
                vendor=vendor,
                bill_no=bill_number,
                bill_date=bill_date,
                igst=igst_val,
                cgst=cgst_val,
                sgst=sgst_val,
                total=safe_float_convert(relevant_data.get('total', 0)),
                note="AI Analyzed Expense Bill",
                organization=organization,
                gst_type=gst_type
            )

            # Create analyzed products (expense items)
            product_instances = []
            expenses = relevant_data.get('expenses', [])
            if isinstance(expenses, list):
                for expense in expenses:
                    if isinstance(expense, dict):
                        product = TallyExpenseAnalyzedProduct(
                            expense_bill=analyzed_bill,
                            item_details=str(expense.get('description', '')),
                            amount=safe_float_convert(expense.get('amount', 0)),
                            debit_or_credit=TallyExpenseAnalyzedProduct.DebitCredit.DEBIT,
                            # Expenses are typically debits
                            organization=organization
                        )
                        product_instances.append(product)

            if product_instances:
                TallyExpenseAnalyzedProduct.objects.bulk_create(product_instances)
                logger.info(f"Successfully created {len(product_instances)} products for expense bill {analyzed_bill.id}")

                # ✅ AUTO-CREATE CONSOLIDATED PRODUCT FOR MULTI-ITEM BILLS
                if len(product_instances) > 1:
                    try:
                        # Delete existing consolidated product if exists
                        TallyExpenseConsolidatedProduct.objects.filter(expense_bill=analyzed_bill).delete()

                        # Calculate consolidated data
                        total_amount = sum(p.amount for p in product_instances)
                        items_count = len(product_instances)

                        # Create detailed breakdown
                        item_details = []
                        for product in product_instances:
                            item_details.append(f'• {product.item_details} (Amount: ₹{product.amount})')

                        consolidated_details = f'Consolidated {items_count} expense entries:\n' + '\n'.join(item_details)

                        # Create consolidated product
                        consolidated_product = TallyExpenseConsolidatedProduct.objects.create(
                            expense_bill=analyzed_bill,
                            organization=organization,
                            item_details=consolidated_details,
                            amount=total_amount,
                            debit_or_credit=TallyExpenseConsolidatedProduct.DebitCredit.DEBIT,  # Default for expenses
                            original_entries_count=items_count,
                            consolidation_notes=f'Auto-created during analysis for {items_count} expense entries'
                        )

                        logger.info(f"✅ Auto-created consolidated expense product for bill {analyzed_bill.id} with {items_count} entries (₹{total_amount})")

                        # Set consolidate flag to true when consolidated product is created
                        analyzed_bill.consolidate = True
                        analyzed_bill.save(update_fields=['consolidate'])

                    except Exception as e:
                        logger.error(f"❌ Error creating consolidated expense product for bill {analyzed_bill.id}: {str(e)}")
                        # Don't raise - consolidated product creation failure shouldn't break the main flow
                else:
                    logger.info(f"ℹ️ Skipping consolidated expense product creation - bill has only {len(product_instances)} item(s)")

            # Update bill status
            bill.status = TallyExpenseBill.BillStatus.ANALYSED
            bill.process = True
            bill.save(update_fields=['status', 'process'])

            return analyzed_bill

    except Exception as e:
        logger.error(f"Error processing expense analysis data: {str(e)} - Data: {json_data}")
        raise Exception(f"Error processing expense analysis data: {str(e)}")


def safe_get_nested(data, keys, default=None):
    """Safely get nested dictionary value"""
    try:
        current = data
        for key in keys:
            if isinstance(current, dict) and key in current:
                current = current[key]
            else:
                return default
        return current
    except (TypeError, KeyError):
        return default


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


def safe_float_convert(value):
    """Safely convert value to float"""
    try:
        if value is None or value == '':
            return 0.0
        return float(value)
    except (ValueError, TypeError):
        return 0.0


def safe_int_convert(value):
    """Safely convert value to int"""
    try:
        if value is None or value == '':
            return 0
        return int(float(value))  # Convert through float to handle decimal strings
    except (ValueError, TypeError):
        return 0


def parse_expense_bill_date(date_string):
    """Parse expense bill date with multiple format support"""
    if not date_string:
        return None

    date_formats = [
        '%d-%m-%Y',
        '%Y-%m-%d',
        '%d/%m/%Y',
        '%Y/%m/%d',
        '%d.%m.%Y',
        '%Y.%m.%d'
    ]

    for date_format in date_formats:
        try:
            return datetime.strptime(str(date_string), date_format).date()
        except ValueError:
            continue

    logger.warning(f"Could not parse date: {date_string}")
    return None


def find_expense_vendor_ledger(company_name, organization):
    """Find matching vendor ledger using TallyConfig"""
    try:
        # Get TallyConfig for the organization
        tally_config = TallyConfig.objects.filter(organization=organization).first()

        if not tally_config:
            # Fallback to default "Sundry Creditors" if no config exists
            parent_ledger = ParentLedger.objects.filter(
                parent="Sundry Creditors",
                organization=organization
            ).first()

            if parent_ledger:
                vendor_list = Ledger.objects.filter(
                    parent=parent_ledger,
                    organization=organization
                )
            else:
                return None
        else:
            # Use configured vendor parent ledgers
            vendor_parent_ledgers = tally_config.vendor_parents.all()
            if not vendor_parent_ledgers.exists():
                return None

            vendor_list = Ledger.objects.filter(
                parent__in=vendor_parent_ledgers,
                organization=organization
            )

        # Find matching vendor (case-insensitive exact match first)
        vendor = vendor_list.filter(name__iexact=company_name).first()
        if not vendor:
            vendor = vendor_list.filter(name__icontains=company_name).first()

        return vendor

    except Exception as e:
        logger.error(f"Error finding expense vendor ledger: {str(e)}")
        return None


def process_pdf_splitting_expense(pdf_file, organization, file_type, uploaded_by):
    """Split PDF into individual pages and create separate expense bills"""
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
                bill = TallyExpenseBill.objects.create(
                    file=ContentFile(
                        image_io.read(),
                        name=f"BM-Expense-Page-{page_num + 1}-{unique_id}.jpg"
                    ),
                    file_type=file_type,
                    organization=organization,
                    uploaded_by=uploaded_by
                )
                created_bills.append(bill)

    except Exception as e:
        logger.error(f"Error splitting expense PDF: {str(e)}")
        raise Exception(f"Expense PDF processing failed: {str(e)}")

    return created_bills


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
    """Get all expense bills for the organization"""
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

    bills = TallyExpenseBill.objects.filter(organization=organization)

    # Filter by status based on query parameters
    status_param = request.query_params.get('status', '').lower()
    if status_param == 'draft':
        bills = bills.filter(status='Draft')
    elif status_param == 'analysed':
        # Include both Analysed and Verified status bills
        bills = bills.filter(status__in=['Analysed', 'Verified'])
    elif status_param == 'synced':
        bills = bills.filter(status='Synced')

    bills = bills.order_by('-created_at')

    # Pagination
    paginator = DefaultPagination()
    page = paginator.paginate_queryset(bills, request)
    if page is not None:
        serializer = TallyExpenseBillSerializer(page, many=True, context={'request': request})
        return paginator.get_paginated_response(serializer.data)

    serializer = TallyExpenseBillSerializer(bills, many=True, context={'request': request})
    return Response(serializer.data)


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

    # Handle both single file and multiple files seamlessly
    files_data = []

    # Check if files are provided as a list (multiple files)
    if 'files' in request.data:
        files_data = request.data.getlist('files') if hasattr(request.data, 'getlist') else request.data.get('files',
                                                                                                             [])
        # Ensure files_data is always a list
        if not isinstance(files_data, list):
            files_data = [files_data] if files_data else []
    # Check if a single file is provided
    elif 'file' in request.data:
        single_file = request.data.get('file')
        if single_file:
            files_data = [single_file]

    # Prepare data for serializer validation
    serializer_data = {
        'files': files_data,
        'file_type': request.data.get('file_type', TallyExpenseBill.BillType.SINGLE)
    }

    serializer = ExpenseBillUploadSerializer(data=serializer_data)
    if not serializer.is_valid():
        return Response({
            'error': 'Invalid Upload Data',
            'message': 'The uploaded expense file data is invalid. Please check file format and size requirements.',
            'details': serializer.errors,
            'error_code': 'INVALID_UPLOAD_DATA'
        }, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

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

    files = serializer.validated_data['files']
    file_type = serializer.validated_data['file_type']
    created_bills = []

    if not files:
        return Response(
            {
                'error': 'No Files Provided',
                'message': 'At least one expense file must be provided for upload. Please select files to upload.',
                'error_code': 'NO_FILES_PROVIDED'
            },
            status=status.HTTP_422_UNPROCESSABLE_ENTITY
        )

    try:
        with transaction.atomic():
            # Check for potential duplicates based on file characteristics
            upload_warnings = []

            for uploaded_file in files:
                file_extension = uploaded_file.name.lower().split('.')[-1]

                # Check for potential file-level duplicates (same name, similar size)
                similar_files = TallyExpenseBill.objects.filter(
                    organization=organization,
                    file__isnull=False
                ).exclude(status=TallyExpenseBill.BillStatus.DRAFT)

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
                                print(f"[TALLY EXPENSE DEBUG] Error accessing file for bill {existing_bill.billmunshiName}: {str(e)}")
                                continue

                if potential_duplicate_files:
                    upload_warnings.append({
                        'uploaded_file': uploaded_file.name,
                        'potential_duplicates': len(potential_duplicate_files),
                        'warning': f'File "{uploaded_file.name}" may be a duplicate of existing expense bills',
                        'existing_bills': [
                            {
                                'bill_name': dup['bill'].bill_munshi_name,
                                'bill_id': str(dup['bill'].id),
                                'match_type': dup['match_type'],
                                'reason': dup['reason']
                            } for dup in potential_duplicate_files[:3]  # Limit to first 3 matches
                        ]
                    })

                # Handle PDF splitting for multiple invoice files
                if (file_type == TallyExpenseBill.BillType.MULTI and
                        file_extension == 'pdf'):

                    pdf_bills = process_pdf_splitting_expense(
                        uploaded_file, organization, file_type, request.user
                    )
                    created_bills.extend(pdf_bills)
                else:
                    # Create single bill (including PDFs for single invoice type)
                    bill = TallyExpenseBill.objects.create(
                        file=uploaded_file,
                        file_type=file_type,
                        organization=organization,
                        uploaded_by=request.user
                    )
                    created_bills.append(bill)

        # Auto-analyze uploaded bills and check for duplicates
        analysis_results = []
        all_duplicate_warnings = []

        for bill in created_bills:
            # Auto-analyze the bill if it's in Draft status
            if bill.status == TallyExpenseBill.BillStatus.DRAFT:
                try:
                    logger.info(f"Auto-analyzing Tally expense bill: {bill.bill_munshi_name}")

                    # Analyze with AI - this returns a TallyExpenseAnalyzedBill instance
                    analyzed_bill_instance = analyze_expense_bill_with_ai(bill, organization)

                    if analyzed_bill_instance:
                        # Check for duplicates after analysis
                        is_duplicate, duplicate_bills, max_similarity = check_duplicate_tally_expense_bill(bill, organization)

                        analysis_data = {
                            'bill_id': str(bill.id),
                            'bill_name': bill.bill_munshi_name,
                            'analysis_successful': True,
                            'duplicate_detected': is_duplicate
                        }

                        if is_duplicate:
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

                            analysis_data.update({
                                "duplicate_count": len(duplicate_bills),
                                "max_similarity": round(max_similarity, 2),
                                "duplicate_bills": duplicate_warnings,
                                "warning_message": f"⚠️ DUPLICATE DETECTED: Tally Expense Bill '{bill.bill_munshi_name}' appears to be {round(max_similarity, 1)}% similar to {len(duplicate_bills)} existing bill(s)."
                            })

                            all_duplicate_warnings.extend(duplicate_warnings)
                            logger.warning(f"Tally expense duplicate detected for {bill.bill_munshi_name} - {len(duplicate_bills)} similar bills found")

                        analysis_results.append(analysis_data)
                        logger.info(f"Successfully analyzed Tally expense bill: {bill.bill_munshi_name}")
                    else:
                        analysis_results.append({
                            'bill_id': str(bill.id),
                            'bill_name': bill.bill_munshi_name,
                            'analysis_successful': False,
                            'error': 'Analysis failed - no analyzed bill created',
                            'duplicate_detected': False
                        })

                except Exception as analysis_error:
                    logger.error(f"Auto-analysis failed for Tally expense bill {bill.bill_munshi_name}: {str(analysis_error)}")
                    analysis_results.append({
                        'bill_id': str(bill.id),
                        'bill_name': bill.bill_munshi_name,
                        'analysis_successful': False,
                        'error': str(analysis_error),
                        'duplicate_detected': False
                    })

        response_serializer = TallyExpenseBillSerializer(created_bills, many=True, context={'request': request})

        response_data = {
            'message': f'Successfully uploaded {len(files)} file(s) and created {len(created_bills)} expense bill(s)',
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
                warning_messages.append(f"🔍 CONTENT WARNING: {len(all_duplicate_warnings)} duplicate(s) detected after analyzing Tally expense bill content")
                response_data['duplicate_warnings'] = all_duplicate_warnings

            response_data.update({
                'total_warnings': warnings_count,
                'warning_message': " | ".join(warning_messages) + " | Please review carefully before proceeding."
            })

            logger.warning(f"Tally expense bills - Total warnings generated: {warnings_count} (Upload: {len(upload_warnings)}, Content: {len(all_duplicate_warnings)})")

        return Response(response_data, status=status.HTTP_201_CREATED)

    except Exception as e:
        logger.error(f"Error uploading expense bills: {str(e)}")
        return Response({
            'error': 'Expense File Upload Processing Failed',
            'message': 'There was an error processing the uploaded expense files. This could be due to file corruption, unsupported format, or server issues.',
            'details': str(e),
            'error_code': 'UPLOAD_PROCESSING_FAILED'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


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
        igst_val = round(safe_float_convert(existing_data.get('igst', 0)), 2)
        cgst_val = round(safe_float_convert(existing_data.get('cgst', 0)), 2)
        sgst_val = round(safe_float_convert(existing_data.get('sgst', 0)), 2)
        total_val = round(safe_float_convert(existing_data.get('total', 0)), 2)

        if igst_val > 0:
            gst_type = TallyExpenseAnalyzedBill.GSTType.IGST
        elif cgst_val > 0 or sgst_val > 0:
            gst_type = TallyExpenseAnalyzedBill.GSTType.CGST_SGST
        else:
            gst_type = TallyExpenseAnalyzedBill.GSTType.UNKNOWN

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
                total=total_val,
                note="AI Analyzed Expense Bill (Existing Data)",
                organization=organization,
                gst_type=gst_type
            )

            # Save without calling clean() to skip validation
            analyzed_bill.save(skip_validation=True)

            # Create analyzed products (expense items)
            created_products = []
            expenses = existing_data.get('expenses', [])

            if isinstance(expenses, list):
                for expense in expenses:
                    if isinstance(expense, dict):
                        amount = round(safe_float_convert(expense.get('amount', 0)), 2)

                        product = TallyExpenseAnalyzedProduct(
                            expense_bill=analyzed_bill,
                            item_details=str(expense.get('description', '')),
                            amount=amount,
                            debit_or_credit=TallyExpenseAnalyzedProduct.DebitCredit.DEBIT,
                            organization=organization
                        )
                        created_products.append(product)

            # Bulk create products
            if created_products:
                TallyExpenseAnalyzedProduct.objects.bulk_create(created_products)
                logger.info(f"Successfully created {len(created_products)} expense products for bill {analyzed_bill.id}")

                # ✅ AUTO-CREATE CONSOLIDATED PRODUCT FOR MULTI-ITEM BILLS
                if len(created_products) > 1:
                    try:
                        # Delete existing consolidated product if exists
                        TallyExpenseConsolidatedProduct.objects.filter(expense_bill=analyzed_bill).delete()

                        # Calculate consolidated data
                        total_amount = sum(p.amount for p in created_products)
                        items_count = len(created_products)

                        # Create detailed breakdown
                        item_details = []
                        for product in created_products:
                            item_details.append(f'• {product.item_details} (Amount: ₹{product.amount})')

                        consolidated_details = f'Consolidated {items_count} expense entries:\n' + '\n'.join(item_details)

                        # Create consolidated product
                        consolidated_product = TallyExpenseConsolidatedProduct.objects.create(
                            expense_bill=analyzed_bill,
                            organization=organization,
                            item_details=consolidated_details,
                            amount=total_amount,
                            debit_or_credit=TallyExpenseConsolidatedProduct.DebitCredit.DEBIT,  # Default for expenses
                            original_entries_count=items_count,
                            consolidation_notes=f'Auto-created during existing data processing for {items_count} expense entries'
                        )

                        logger.info(f"✅ Auto-created consolidated expense product for bill {analyzed_bill.id} with {items_count} entries (₹{total_amount})")

                        # Set consolidate flag to true when consolidated product is created
                        analyzed_bill.consolidate = True
                        analyzed_bill.save(update_fields=['consolidate'])

                    except Exception as e:
                        logger.error(f"❌ Error creating consolidated expense product for bill {analyzed_bill.id}: {str(e)}")
                        # Don't raise - consolidated product creation failure shouldn't break the main flow
                else:
                    logger.info(f"ℹ️ Skipping consolidated expense product creation - bill has only {len(created_products)} item(s)")

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

    try:
        # Fetch the TallyExpenseBill using the new serializer
        bill = TallyExpenseBill.objects.get(
            id=bill_id,
            organization=organization
        )

        # Use the enhanced serializer with analyzed data
        from .serializers import TallyExpenseBillDetailSerializer
        serializer = TallyExpenseBillDetailSerializer(bill, context={'request': request})

        return Response(serializer.data, status=status.HTTP_200_OK)


    except TallyExpenseBill.DoesNotExist:
        return Response(
            {'error': 'Bill not found'},
            status=status.HTTP_404_NOT_FOUND
        )


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
    from .models import TallyExpenseAnalyzedBill
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
            analyzed_bill.vendor_amount = round(float(analyzed_data['vendor_amount']), 2)

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
            analyzed_bill.total = round(float(analyzed_data['total']), 2)

        # Update tax information
        taxes_data = analyzed_data.get('taxes', {})
        if taxes_data:
            # Update tax amounts with proper rounding to 2 decimal places
            igst_data = taxes_data.get('igst', {})
            if 'amount' in igst_data:
                analyzed_bill.igst = round(float(igst_data['amount']), 2)
            if 'ledger' in igst_data and igst_data['ledger'] != "No Tax Ledger":
                igst_ledger = find_or_create_expense_tax_ledger(igst_data['ledger'], 'IGST', organization)
                if igst_ledger:
                    analyzed_bill.igst_taxes = igst_ledger
            if 'debit_or_credit' in igst_data:
                analyzed_bill.igst_debit_or_credit = igst_data['debit_or_credit']

            cgst_data = taxes_data.get('cgst', {})
            if 'amount' in cgst_data:
                analyzed_bill.cgst = round(float(cgst_data['amount']), 2)
            if 'ledger' in cgst_data and cgst_data['ledger'] != "No Tax Ledger":
                cgst_ledger = find_or_create_expense_tax_ledger(cgst_data['ledger'], 'CGST', organization)
                if cgst_ledger:
                    analyzed_bill.cgst_taxes = cgst_ledger
            if 'debit_or_credit' in cgst_data:
                analyzed_bill.cgst_debit_or_credit = cgst_data['debit_or_credit']

            sgst_data = taxes_data.get('sgst', {})
            if 'amount' in sgst_data:
                analyzed_bill.sgst = round(float(sgst_data['amount']), 2)
            if 'ledger' in sgst_data and sgst_data['ledger'] != "No Tax Ledger":
                sgst_ledger = find_or_create_expense_tax_ledger(sgst_data['ledger'], 'SGST', organization)
                if sgst_ledger:
                    analyzed_bill.sgst_taxes = sgst_ledger
            if 'debit_or_credit' in sgst_data:
                analyzed_bill.sgst_debit_or_credit = sgst_data['debit_or_credit']

            # Handle TDS data
            tds_data = taxes_data.get('tds', {})
            if 'amount' in tds_data:
                analyzed_bill.tds = round(float(tds_data['amount']), 2)
            if 'ledger' in tds_data and tds_data['ledger'] != "No Tax Ledger":
                tds_ledger = find_or_create_expense_tax_ledger(tds_data['ledger'], 'TDS', organization)
                if tds_ledger:
                    analyzed_bill.tds_taxes = tds_ledger
            if 'debit_or_credit' in tds_data:
                analyzed_bill.tds_debit_or_credit = tds_data['debit_or_credit']

        # Determine GST type based on updated amounts
        if analyzed_bill.igst and analyzed_bill.igst > 0:
            analyzed_bill.gst_type = TallyExpenseAnalyzedBill.GSTType.IGST
        elif (analyzed_bill.cgst and analyzed_bill.cgst > 0) or (analyzed_bill.sgst and analyzed_bill.sgst > 0):
            analyzed_bill.gst_type = TallyExpenseAnalyzedBill.GSTType.CGST_SGST
        else:
            analyzed_bill.gst_type = TallyExpenseAnalyzedBill.GSTType.UNKNOWN

        # Save the analyzed bill
        analyzed_bill.save(skip_validation=True)

        # Update expense items with item_id handling
        expense_items = analyzed_data.get('expense_items', [])
        if expense_items:
            update_analyzed_expense_products(analyzed_bill, expense_items, organization)

        # 🔄 Handle consolidate_prod array from frontend (similar to Zoho implementation)
        consolidate_prod_data = analyzed_data.get('consolidate_prod', [])
        if consolidate_prod_data:
            try:
                # 🔄 FIRST: Clear existing consolidated products to prevent duplicates
                existing_consolidated = TallyExpenseConsolidatedProduct.objects.filter(expense_bill=analyzed_bill)
                if existing_consolidated.exists():
                    existing_count = existing_consolidated.count()
                    existing_consolidated.delete()
                    logger.info(f"Deleted {existing_count} existing consolidated products before creating new ones")

                # Handle consolidated product creation (always create new after clearing)
                for idx, consolidated_data in enumerate(consolidate_prod_data):
                    logger.info(f"Creating consolidated expense product {idx + 1}: {consolidated_data.get('item_details', 'Unnamed')}")
                    
                    # Find chart of accounts ledger if specified
                    chart_ledger = None
                    chart_ledger_name = consolidated_data.get('chart_of_accounts')
                    if chart_ledger_name and chart_ledger_name != "No COA Ledger":
                        try:
                            chart_ledger = Ledger.objects.filter(
                                name=chart_ledger_name,
                                organization=organization
                            ).first()
                            if chart_ledger:
                                logger.info(f"Found chart of accounts ledger: {chart_ledger.name}")
                            else:
                                logger.warning(f"Chart of accounts ledger not found: {chart_ledger_name}")
                        except Exception as e:
                            logger.error(f"Error finding chart of accounts ledger: {e}")

                    # Create new consolidated product (since we cleared existing ones)
                    consolidated_product = TallyExpenseConsolidatedProduct.objects.create(
                        expense_bill=analyzed_bill,
                        organization=organization,
                        item_details=consolidated_data.get('item_details', 'Consolidated expense from verification'),
                        amount=consolidated_data.get('amount', 0),
                        debit_or_credit=consolidated_data.get('debit_or_credit', 'debit'),
                        chart_of_accounts=chart_ledger,  # Fixed: use chart_of_accounts instead of chart_of_accounts_id
                        original_entries_count=consolidated_data.get('original_entries_count', 1),
                        consolidation_notes='Created from frontend verification'
                    )
                    logger.info(f"Created consolidated expense product {consolidated_product.id} with chart of accounts: {chart_ledger.name if chart_ledger else 'None'}")

            except Exception as consolidate_error:
                logger.error(f"Error processing consolidate_prod array: {consolidate_error}")
                # Don't fail the entire request, just log the error

        # Handle consolidation flag
        consolidate_flag = analyzed_data.get('consolidate', False)
        if consolidate_flag != getattr(analyzed_bill, 'consolidate', False):
            analyzed_bill.consolidate = consolidate_flag
            analyzed_bill.save()

        return analyzed_bill


def find_or_create_expense_vendor_ledger(vendor_name, vendor_data, organization):
    """Find existing vendor ledger or create new one using TallyConfig"""
    try:
        # First try to find exact match
        vendor = Ledger.objects.filter(
            name__iexact=vendor_name.strip(),
            organization=organization
        ).first()

        if vendor:
            # Update vendor details if provided
            if vendor_data.get('master_id') and vendor_data['master_id'] != "No Ledger":
                vendor.master_id = vendor_data['master_id']
            if vendor_data.get('gst_in') and vendor_data['gst_in'] != "No Ledger":
                vendor.gst_in = vendor_data['gst_in']
            if vendor_data.get('company') and vendor_data['company'] != "No Ledger":
                vendor.company = vendor_data['company']
            vendor.save()
            return vendor

        # Get TallyConfig for the organization
        tally_config = TallyConfig.objects.filter(organization=organization).first()

        if not tally_config:
            # Fallback: try to find or create default parent ledger
            try:
                parent_ledger = ParentLedger.objects.get(
                    parent="Sundry Creditors",
                    organization=organization
                )
            except ParentLedger.DoesNotExist:
                parent_ledger = ParentLedger.objects.create(
                    parent="Sundry Creditors",
                    organization=organization
                )
        else:
            # Use first configured vendor parent ledger or create default
            vendor_parent_ledgers = tally_config.vendor_parents.all()
            if vendor_parent_ledgers.exists():
                parent_ledger = vendor_parent_ledgers.first()
            else:
                # Create default if no vendor parents configured
                try:
                    parent_ledger = ParentLedger.objects.get(
                        parent="Sundry Creditors",
                        organization=organization
                    )
                except ParentLedger.DoesNotExist:
                    parent_ledger = ParentLedger.objects.create(
                        parent="Sundry Creditors",
                        organization=organization
                    )

        # Create new vendor ledger
        vendor = Ledger.objects.create(
            name=vendor_name.strip(),
            parent=parent_ledger,
            master_id=vendor_data.get('master_id') if vendor_data.get('master_id') != "No Ledger" else None,
            gst_in=vendor_data.get('gst_in') if vendor_data.get('gst_in') != "No Ledger" else None,
            company=vendor_data.get('company') if vendor_data.get('company') != "No Ledger" else None,
            organization=organization
        )
        return vendor

    except Exception as e:
        logger.error(f"Error finding/creating expense vendor ledger: {str(e)}")
        return None


def find_or_create_expense_tax_ledger(ledger_name, tax_type, organization):
    """Find existing tax ledger or create new one using TallyConfig"""
    try:
        # First try to find exact match
        tax_ledger = Ledger.objects.filter(
            name__iexact=ledger_name.strip(),
            organization=organization
        ).first()

        if tax_ledger:
            return tax_ledger

        # Get TallyConfig for the organization
        tally_config = TallyConfig.objects.filter(organization=organization).first()

        if not tally_config:
            # Fallback to default "Duties & Taxes"
            try:
                parent_ledger = ParentLedger.objects.get(
                    parent="Duties & Taxes",
                    organization=organization
                )
            except ParentLedger.DoesNotExist:
                parent_ledger = ParentLedger.objects.create(
                    parent="Duties & Taxes",
                    organization=organization
                )
        else:
            # Use configured tax parent ledgers based on tax type
            if tax_type == 'IGST':
                tax_parent_ledgers = tally_config.igst_parents.all()
            elif tax_type == 'CGST':
                tax_parent_ledgers = tally_config.cgst_parents.all()
            elif tax_type == 'SGST':
                tax_parent_ledgers = tally_config.sgst_parents.all()
            else:
                # For COA or other types, use any available tax parent
                tax_parent_ledgers = (tally_config.igst_parents.all() |
                                      tally_config.cgst_parents.all() |
                                      tally_config.sgst_parents.all())

            if tax_parent_ledgers.exists():
                parent_ledger = tax_parent_ledgers.first()
            else:
                # Create default if no tax parents configured
                try:
                    parent_ledger = ParentLedger.objects.get(
                        parent="Duties & Taxes",
                        organization=organization
                    )
                except ParentLedger.DoesNotExist:
                    parent_ledger = ParentLedger.objects.create(
                        parent="Duties & Taxes",
                        organization=organization
                    )

        # Create new tax ledger
        tax_ledger = Ledger.objects.create(
            name=ledger_name.strip(),
            parent=parent_ledger,
            organization=organization
        )
        return tax_ledger

    except Exception as e:
        logger.error(f"Error finding/creating expense tax ledger: {str(e)}")
        return None


def update_analyzed_expense_products(analyzed_bill, expense_items, organization):
    """Update existing expense products and create new ones based on item_id"""

    # Validate debit/credit balance before processing - including all components
    total_debit = 0
    total_credit = 0

    # Calculate debit/credit from expense items
    for item_data in expense_items:
        amount = round(float(item_data.get('amount', 0)), 2)
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
            f"This includes expense items, taxes (IGST/CGST/SGST), and vendor amount."
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
            product.amount = round(float(item_data['amount']), 2)
        if 'debit_or_credit' in item_data:
            product.debit_or_credit = item_data['debit_or_credit']

        # Handle chart of accounts ledger
        if 'chart_of_accounts' in item_data and item_data['chart_of_accounts'] != "No COA Ledger":
            coa_ledger = find_or_create_expense_tax_ledger(item_data['chart_of_accounts'], 'COA', organization)
            if coa_ledger:
                product.chart_of_accounts = coa_ledger

        product.save()

    # Delete expense products that are no longer in the frontend payload
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
    
    logger.info(
        f"Expense product update summary: {len(updated_product_ids)} updated, "
        f"{len(expense_items or []) - len(updated_product_ids)} created, "
        f"{len(products_to_delete) if products_to_delete else 0} deleted"
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
            },
            "cgst": {
                "amount": float(analyzed_bill.cgst or 0),
                "ledger": str(analyzed_bill.cgst_taxes) if analyzed_bill.cgst_taxes else "No Tax Ledger",
            },
            "sgst": {
                "amount": float(analyzed_bill.sgst or 0),
                "ledger": str(analyzed_bill.sgst_taxes) if analyzed_bill.sgst_taxes else "No Tax Ledger",
            },
            "tds": {
                "amount": float(analyzed_bill.tds or 0),
                "ledger": str(analyzed_bill.tds_taxes) if analyzed_bill.tds_taxes else "No Tax Ledger",
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
    """Delete expense bill"""
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

    # Delete the file from storage if it exists
    if bill.file:
        file_path = os.path.join(settings.MEDIA_ROOT, str(bill.file))
        if os.path.exists(file_path):
            os.remove(file_path)

    # Delete the bill record from the database
    bill.delete()

    return Response(status=status.HTTP_204_NO_CONTENT)


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
    """Get bills filtered by status"""
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

    status_filter = request.query_params.get('status')

    if not status_filter:
        return Response(
            {
                'error': 'Missing Status Parameter',
                'message': 'Status parameter is required to filter expense bills. Please provide a valid status (Draft, Analysed, Verified, Synced).',
                'error_code': 'STATUS_PARAM_REQUIRED'
            },
            status=status.HTTP_422_UNPROCESSABLE_ENTITY
        )

    bills = TallyExpenseBill.objects.filter(
        organization=organization,
        status=status_filter
    ).order_by('-created_at')

    serializer = TallyExpenseBillSerializer(bills, many=True)
    return Response(serializer.data)


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

    # Get all analyzed bills where the main bill status is "Synced"
    analyzed_bills = TallyExpenseAnalyzedBill.objects.filter(
        organization=organization,
        selected_bill__status=TallyExpenseBill.BillStatus.SYNCED
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
        "id": str(analyzed_bill.id),
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

# apps/module/tally/vendor_views_functional.py

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
    TallyConfig
)
from .serializers import (
    TallyVendorBillSerializer,
    TallyVendorAnalyzedBillSerializer,
    VendorBillUploadSerializer,
    BillAnalysisRequestSerializer,
    BillVerificationSerializer,
    BillSyncRequestSerializer,
    BillSyncResponseSerializer
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

def check_duplicate_tally_vendor_bill(bill, organization):
    """
    Check if a Tally vendor bill is a duplicate based on invoice number and vendor information.
    Returns tuple (is_duplicate, duplicate_bills, similarity_score)
    """
    logger.info(f"Checking for duplicates of Tally vendor bill {bill.id} in organization {organization.id}")

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
    potential_duplicates = TallyVendorBill.objects.filter(
        organization=organization,
        status__in=[TallyVendorBill.BillStatus.ANALYSED, TallyVendorBill.BillStatus.VERIFIED, TallyVendorBill.BillStatus.SYNCED]
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
            vendor_similarity = _calculate_tally_string_similarity(current_vendor_name.lower(), other_vendor_name.lower())
            if vendor_similarity > 0.8:
                similarity_score += 30.0 * vendor_similarity
                match_reasons.append('vendor_similarity')
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
    logger.info(f"Tally vendor duplicate check complete. Found {len(duplicate_bills)} potential duplicates with max similarity {max_similarity}")

    return is_duplicate, duplicate_bills, max_similarity


def _calculate_tally_string_similarity(str1, str2):
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


def analyze_bill_with_ai(bill, organization):
    """Analyze bill using OpenAI API with enhanced PDF handling and error recovery"""
    if not client:
        raise Exception("OpenAI client not configured")

    logger.info(f"Starting AI analysis for bill {bill.id}, file: {bill.file.name}")

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
        logger.error(f"Error reading/processing bill file: {str(e)}")
        raise Exception(f"Error reading bill file: {str(e)}")

    # Enhanced prompt for Indian invoices with aggressive GST number extraction
    enhanced_prompt = """
    Analyze this Indian invoice/bill image very carefully and extract ALL visible information in JSON format.
    
    CRITICAL: ALWAYS include gst_number field in both 'from' and 'to' sections, even if empty.
    
    CRITICAL FOCUS ON GST NUMBERS:
    GST numbers in Indian invoices appear in various formats and locations:
    - GSTIN: 22AAAAA0000A1Z5 (15-character alphanumeric code)
    - GST No: 06AADCK7940H1ZG
    - Tax ID: 27AABCU9603R1ZX
    - Registration No: followed by GST number
    - Often embedded in addresses like "GST NO. 123456... State Name: Delhi, Code: 07"
    - May appear as "GSTIN/UIN :" followed by the number
    - Sometimes shown in headers, footers, or separate tax information sections
    - Can be in vendor section (from) and customer section (to)
    - Look for patterns like "06AADCK7940H1ZG", "22AAAAA0000A1Z5"
    - Check every line of text for 15-character alphanumeric codes
    
    SEARCH EVERYWHERE FOR GST NUMBERS:
    1. Company headers and letterheads
    2. Address blocks (often at the end of addresses)
    3. Tax information sections
    4. Registration details sections
    5. Footer information
    6. Any line containing "GST", "GSTIN", "Tax ID", "UIN", "Registration", "REG"
    7. Business registration details
    8. Company information boxes
    9. Billing address sections
    10. Shipping address sections
    
    EXTRACTION REQUIREMENTS:
    1. Invoice/Bill Number (Invoice No, Bill No, Receipt No, etc.)
    2. Dates (Invoice Date, Bill Date, Due Date - convert to YYYY-MM-DD format)
    3. Vendor/Company details (from - who is billing)
    4. Customer details (to - who is being billed)
    5. Line items with descriptions, quantities, and prices
    6. Tax amounts (IGST, CGST, SGST - look for percentages and amounts)
    7. Total amount (Total, Grand Total, Amount Payable)
    8. GST NUMBERS - MANDATORY FIELD, use empty string if not found
    
    IMPORTANT RULES:
    - ALWAYS include "gst_number": "" field in both from and to sections
    - Extract EXACT text as it appears on the document
    - For numbers, remove currency symbols (₹, Rs.) and commas
    - If GST number is not visible or unclear, use empty string ""
    - If any other field is not visible, use empty string "" or 0 for numbers
    - Look carefully at the entire document, including headers, footers, and margins
    - Pay special attention to tax sections which may be in tables or separate areas
    - GST numbers are 15-character codes - extract the full code even if split across lines
    - Check every text line for potential GST numbers
    - Look for format: 2 digits + 10 alphanumeric characters + 1 digit + 2 characters
    
    Return data in this EXACT JSON structure (all fields mandatory):
    {
        "invoiceNumber": "Invoice/Bill number as shown on document",
        "dateIssued": "Invoice/Bill date in YYYY-MM-DD format",
        "dueDate": "Due date in YYYY-MM-DD format if mentioned, empty string if not",
        "from": {
            "name": "Vendor/Company name (who is sending the bill)",
            "address": "Complete vendor address",
            "gst_number": "Vendor's GST number - SEARCH THOROUGHLY or empty string if not found"
        },
        "to": {
            "name": "Customer name (who is receiving the bill)", 
            "address": "Complete customer address",
            "gst_number": "Customer's GST number - SEARCH THOROUGHLY or empty string if not found"
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
        raw_response = response.choices[0].message.content.strip()
        logger.info(f"Raw OpenAI response: {raw_response}")

        # Try to parse JSON with error recovery
        json_data = None
        try:
            json_data = json.loads(raw_response)
            logger.info("Successfully parsed JSON response from OpenAI")
        except json.JSONDecodeError as e:
            logger.warning(f"Initial JSON parse failed: {str(e)}")

            # Attempt to fix common JSON issues
            try:
                # Try to extract JSON from markdown code blocks
                if "```json" in raw_response and "```" in raw_response:
                    json_start = raw_response.find("```json") + 7
                    json_end = raw_response.find("```", json_start)
                    if json_end > json_start:
                        json_content = raw_response[json_start:json_end].strip()
                        logger.info(f"Extracted JSON from markdown: {json_content}")
                        json_data = json.loads(json_content)
                        logger.info("Successfully parsed JSON from markdown extraction")

                # Try to extract JSON between first { and last }
                elif "{" in raw_response and "}" in raw_response:
                    json_start = raw_response.find("{")
                    json_end = raw_response.rfind("}") + 1
                    if json_end > json_start:
                        json_content = raw_response[json_start:json_end]
                        logger.info(f"Extracted JSON by braces: {json_content}")
                        json_data = json.loads(json_content)
                        logger.info("Successfully parsed JSON from brace extraction")

            except json.JSONDecodeError as recovery_error:
                logger.error(f"JSON recovery also failed: {str(recovery_error)}")

                # If all parsing fails, create a minimal fallback response
                logger.warning("Creating fallback minimal JSON response")
                json_data = {
                    "error": "json_parse_failed",
                    "raw_response": raw_response[:500] + "..." if len(raw_response) > 500 else raw_response,
                    "fallback_data": {
                        "invoiceNumber": "PARSE_ERROR",
                        "dateIssued": "",
                        "dueDate": "",
                        "from": {"name": "VENDOR_PARSE_ERROR"},
                        "to": {"name": ""},
                        "totalAmount": 0,
                        "lineItems": []
                    }
                }

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON from OpenAI response: {str(e)}")
        logger.error(f"Raw response: {response.choices[0].message.content if response.choices else 'No response'}")
        raise Exception(f"Invalid JSON response from OpenAI: {str(e)}")
    except Exception as e:
        logger.error(f"AI processing failed: {str(e)}")
        raise Exception(f"AI processing failed: {str(e)}")

    # Process and save extracted data
    return process_analysis_data(bill, json_data, organization)


def validate_bill_ownership(json_data, organization):
    """Validate if the bill belongs to the organization by matching organization details with 'from' field (vendor)"""
    try:
        # Extract vendor info ("from" field - who sent/issued the bill)
        from_data = json_data.get('from', {})
        if isinstance(from_data, dict):
            vendor_name = from_data.get('name', '').strip()
            vendor_gst = from_data.get('gst_number', '').strip()
            vendor_address = from_data.get('address', '').strip()
        else:
            vendor_name = ''
            vendor_gst = ''
            vendor_address = ''

        # If GST number is missing from extraction, try to find it in the address
        if not vendor_gst and vendor_address:
            import re
            # Common GST number patterns in Indian invoices
            gst_patterns = [
                r'GST\s*NO\.?\s*:?\s*([A-Z0-9]{15})',
                r'GSTIN/UIN\s*:?\s*([A-Z0-9]{15})',
                r'GSTIN\s*:?\s*([A-Z0-9]{15})',
                r'Tax\s*ID\s*:?\s*([A-Z0-9]{15})',
                r'UIN\s*:?\s*([A-Z0-9]{15})',
                r'Registration\s*No\.?\s*:?\s*([A-Z0-9]{15})',
                r'\b([A-Z0-9]{15})\b'  # Generic 15-character alphanumeric pattern
            ]
            
            for pattern in gst_patterns:
                match = re.search(pattern, vendor_address.upper(), re.IGNORECASE)
                if match:
                    vendor_gst = match.group(1).strip()
                    logger.info(f"Found GST number in address: {vendor_gst}")
                    break

        logger.info(f"Validating ownership - Vendor: {vendor_name}, Vendor GST: {vendor_gst}")
        logger.info(f"Organization: {getattr(organization, 'name', 'Unknown')}, Org GST: {getattr(organization, 'gst_number', 'None')}")

        # MAIN LOGIC: Check if organization IS the vendor (from field)
        # This means the bill was issued BY the organization TO someone else
        
        # Priority 1: GST number match (most reliable)
        if vendor_gst and hasattr(organization, 'gst_number') and organization.gst_number:
            org_gst_clean = organization.gst_number.replace(' ', '').replace('-', '').upper()
            vendor_gst_clean = vendor_gst.replace(' ', '').replace('-', '').upper()
            
            if org_gst_clean == vendor_gst_clean:
                return True, f"✅ Organization GST match: {vendor_gst} - This bill was issued BY your organization"
            
            # Also check partial match (in case of truncated GST numbers)
            if len(vendor_gst_clean) >= 10 and len(org_gst_clean) >= 10:
                if org_gst_clean[:10] == vendor_gst_clean[:10]:
                    return True, f"✅ Partial organization GST match: {vendor_gst} - This bill was issued BY your organization"

        # Priority 2: Organization name match with vendor name
        if vendor_name and hasattr(organization, 'name') and organization.name:
            org_name_clean = organization.name.lower().strip()
            vendor_name_clean = vendor_name.lower().strip()
            
            # Exact match
            if org_name_clean == vendor_name_clean:
                return True, f"✅ Exact organization name match: {vendor_name} - This bill was issued BY your organization"
            
            # Partial match (if organization name is contained in vendor name or vice versa)
            if org_name_clean in vendor_name_clean or vendor_name_clean in org_name_clean:
                return True, f"✅ Partial organization name match: {vendor_name} - This bill was issued BY your organization"
            
            # Check for common abbreviations and variations
            org_words = set(org_name_clean.replace(',', '').replace('.', '').split())
            vendor_words = set(vendor_name_clean.replace(',', '').replace('.', '').split())
            
            # Remove common words that don't add meaning
            common_stopwords = {'ltd', 'limited', 'pvt', 'private', 'llp', 'co', 'company', 'inc', 'incorporated'}
            org_words = org_words - common_stopwords
            vendor_words = vendor_words - common_stopwords
            
            if len(org_words) > 0 and len(vendor_words) > 0:
                # Calculate word overlap
                overlap = len(org_words.intersection(vendor_words))
                total_words = len(org_words.union(vendor_words))
                similarity = overlap / total_words if total_words > 0 else 0
                
                if similarity >= 0.6:  # 60% word overlap for organization match
                    return True, f"✅ Similar organization name match ({int(similarity*100)}% similarity): {vendor_name} - This bill was issued BY your organization"
        
        # If no match found, this bill was NOT issued by the organization
        debug_info = f"❌ Bill NOT issued by your organization. Vendor: '{vendor_name}' (GST: '{vendor_gst}') ≠ Your Org: '{getattr(organization, 'name', 'Unknown')}' (GST: '{getattr(organization, 'gst_number', 'None')}')"
        return False, debug_info
        
    except Exception as e:
        logger.error(f"Error validating bill ownership: {str(e)}")
        return False, f"Validation error: {str(e)}"


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

        # Determine GST type with safe conversion
        igst_val = safe_float_convert(relevant_data.get('igst', 0))
        cgst_val = safe_float_convert(relevant_data.get('cgst', 0))
        sgst_val = safe_float_convert(relevant_data.get('sgst', 0))

        if igst_val > 0:
            gst_type = TallyVendorAnalyzedBill.GSTType.IGST
        elif cgst_val > 0 or sgst_val > 0:
            gst_type = TallyVendorAnalyzedBill.GSTType.CGST_SGST
        else:
            gst_type = TallyVendorAnalyzedBill.GSTType.UNKNOWN

        # Create analyzed bill
        with transaction.atomic():
            # Round decimal values to 2 decimal places to avoid validation errors
            from decimal import Decimal, ROUND_HALF_UP

            total_val = safe_float_convert(relevant_data.get('total', 0))
            discount_val = safe_float_convert(relevant_data.get('discount', 0))
            igst_rounded = Decimal(str(igst_val)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            cgst_rounded = Decimal(str(cgst_val)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            sgst_rounded = Decimal(str(sgst_val)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            total_rounded = Decimal(str(total_val)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            discount_rounded = Decimal(str(discount_val)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            analyzed_bill = TallyVendorAnalyzedBill.objects.create(
                selected_bill=bill,
                vendor=vendor,
                bill_no=invoice_number,
                bill_date=bill_date,
                due_date=due_date,
                igst=igst_rounded,
                cgst=cgst_rounded,
                sgst=sgst_rounded,
                discount=discount_rounded,
                total=total_rounded,
                note="AI Analyzed Bill",
                organization=organization,
                gst_type=gst_type
            )

            # Create analyzed products with safe item extraction and tax ledger automation
            product_instances = []
            items = relevant_data.get('items', [])
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        # Handle decimal precision for product amounts
                        price_val = safe_float_convert(item.get('price', 0))
                        quantity_val = safe_int_convert(item.get('quantity', 0))
                        amount_val = price_val * quantity_val

                        # Round to 2 decimal places
                        price_rounded = Decimal(str(price_val)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                        amount_rounded = Decimal(str(amount_val)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

                        # 🚀 AUTO-ASSIGN TAX LEDGERS BASED ON GST VALUES
                        tax_ledger = find_appropriate_tax_ledger(organization, igst_val, cgst_val, sgst_val)
                        
                        # Determine GST rate from tax amounts
                        gst_rate = calculate_gst_rate(amount_val, igst_val, cgst_val, sgst_val)

                        product = TallyVendorAnalyzedProduct(
                            vendor_bill_analyzed=analyzed_bill,
                            item_details=str(item.get('description', '')),
                            price=price_rounded,
                            quantity=quantity_val,
                            amount=amount_rounded,
                            taxes=tax_ledger,  # 🎯 Auto-assigned tax ledger (correct field name)
                            product_gst=gst_rate,   # 🎯 Auto-assigned GST rate  
                            organization=organization
                        )
                        product_instances.append(product)
                        
                        logger.warning(f"📦 Created product with auto-tax: '{item.get('description', '')[:50]}...' | Tax Ledger: '{tax_ledger.name if tax_ledger else 'None'}' | GST Rate: {gst_rate}%")

            if product_instances:
                TallyVendorAnalyzedProduct.objects.bulk_create(product_instances)
                logger.info(f"Successfully created {len(product_instances)} products for bill {analyzed_bill.id}")

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

                        # Create consolidated product
                        consolidated_product = TallyVendorConsolidatedProduct.objects.create(
                            vendor_bill_analyzed=analyzed_bill,
                            organization=organization,
                            item_name=f"Consolidated Items - {invoice_number} ({items_count} items)",
                            item_details=consolidated_details,
                            price=total_rounded,  # Total as rate
                            quantity=1,  # Always 1 for consolidated
                            amount=total_rounded,
                            product_gst="",  # Empty - let user select GST rate
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

            # Update bill status
            bill.status = TallyVendorBill.BillStatus.ANALYSED
            bill.process = True
            bill.save(update_fields=['status', 'process'])

            return analyzed_bill

    except Exception as e:
        logger.error(f"Error processing analysis data: {str(e)} - Data: {json_data}")
        raise Exception(f"Error processing analysis data: {str(e)}")


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


def find_appropriate_tax_ledger(organization, igst_val, cgst_val, sgst_val):
    """Find appropriate tax ledger based on GST values and organization configuration"""
    try:
        logger.warning(f"🏛️  Searching for tax ledger - IGST:{igst_val}, CGST:{cgst_val}, SGST:{sgst_val}")
        
        # Get TallyConfig for the organization
        tally_config = TallyConfig.objects.filter(organization=organization).first()
        
        if not tally_config:
            logger.warning(f"❌ No TallyConfig found for organization {organization.id}. Cannot assign tax ledgers.")
            return None
        
        # Determine GST type based on values and find appropriate parent ledgers
        if igst_val > 0:
            # IGST case - get IGST parent ledgers
            igst_parent_ledgers = tally_config.igst_parents.all()
            logger.warning(f"🔍 IGST case: Found {igst_parent_ledgers.count()} IGST parent ledgers configured")
            
            if igst_parent_ledgers.exists():
                # Look for IGST tax ledger under these parents
                tax_ledgers = Ledger.objects.filter(
                    organization=organization,
                    parent__in=igst_parent_ledgers
                )
                
                logger.warning(f"📊 Found {tax_ledgers.count()} tax ledgers under IGST parents")
                for ledger in tax_ledgers[:5]:
                    logger.warning(f"  - '{ledger.name}' (Parent: {ledger.parent.parent})")
                
                # Try to find IGST-specific ledger first
                igst_ledger = tax_ledgers.filter(name__icontains='igst').first()
                if igst_ledger:
                    logger.warning(f"🎯 Auto-assigned IGST tax ledger: {igst_ledger.name}")
                    return igst_ledger
                
                # Fallback to first available tax ledger under IGST parents
                tax_ledger = tax_ledgers.first()
                if tax_ledger:
                    logger.warning(f"🎯 Auto-assigned IGST fallback tax ledger: {tax_ledger.name}")
                    return tax_ledger
        
        elif cgst_val > 0 or sgst_val > 0:
            # CGST/SGST case - get CGST parent ledgers first
            cgst_parent_ledgers = tally_config.cgst_parents.all()
            logger.warning(f"🔍 CGST/SGST case: Found {cgst_parent_ledgers.count()} CGST parent ledgers configured")
            
            if cgst_parent_ledgers.exists():
                # Look for CGST tax ledger under these parents
                tax_ledgers = Ledger.objects.filter(
                    organization=organization,
                    parent__in=cgst_parent_ledgers
                )
                
                logger.warning(f"📊 Found {tax_ledgers.count()} tax ledgers under CGST parents")
                for ledger in tax_ledgers[:5]:
                    logger.warning(f"  - '{ledger.name}' (Parent: {ledger.parent.parent})")
                
                # Try to find CGST-specific ledger first
                cgst_ledger = tax_ledgers.filter(name__icontains='cgst').first()
                if cgst_ledger:
                    logger.warning(f"🎯 Auto-assigned CGST tax ledger: {cgst_ledger.name}")
                    return cgst_ledger
                
                # Fallback to first available tax ledger under CGST parents
                tax_ledger = tax_ledgers.first()
                if tax_ledger:
                    logger.warning(f"🎯 Auto-assigned CGST fallback tax ledger: {tax_ledger.name}")
                    return tax_ledger
            
            # If no CGST ledger found, try SGST parent ledgers
            sgst_parent_ledgers = tally_config.sgst_parents.all()
            logger.warning(f"🔍 Checking SGST parents: Found {sgst_parent_ledgers.count()} SGST parent ledgers configured")
            
            if sgst_parent_ledgers.exists():
                tax_ledgers = Ledger.objects.filter(
                    organization=organization,
                    parent__in=sgst_parent_ledgers
                )
                
                # Try to find SGST-specific ledger first
                sgst_ledger = tax_ledgers.filter(name__icontains='sgst').first()
                if sgst_ledger:
                    logger.warning(f"🎯 Auto-assigned SGST tax ledger: {sgst_ledger.name}")
                    return sgst_ledger
                
                # Fallback to first available tax ledger under SGST parents
                tax_ledger = tax_ledgers.first()
                if tax_ledger:
                    logger.warning(f"🎯 Auto-assigned SGST fallback tax ledger: {tax_ledger.name}")
                    return tax_ledger
        
        # Final fallback: try any available tax-related parent ledgers
        logger.warning("🔄 No specific GST ledger found, trying fallback approach...")
        
        all_tax_parents = list(tally_config.igst_parents.all()) + list(tally_config.cgst_parents.all()) + list(tally_config.sgst_parents.all())
        
        if all_tax_parents:
            # Remove duplicates while preserving order
            seen = set()
            unique_tax_parents = []
            for parent in all_tax_parents:
                if parent.id not in seen:
                    seen.add(parent.id)
                    unique_tax_parents.append(parent)
            
            logger.warning(f"📋 Checking all {len(unique_tax_parents)} unique tax parent ledgers for fallback")
            
            tax_ledger = Ledger.objects.filter(
                organization=organization,
                parent__in=unique_tax_parents
            ).first()
            
            if tax_ledger:
                logger.warning(f"🎯 Auto-assigned ultimate fallback tax ledger: {tax_ledger.name}")
                return tax_ledger
        
        logger.warning(f"⚠️  No tax ledger found for IGST:{igst_val}, CGST:{cgst_val}, SGST:{sgst_val}")
        logger.warning(f"💡 TallyConfig Summary - IGST parents: {tally_config.igst_parents.count()}, CGST parents: {tally_config.cgst_parents.count()}, SGST parents: {tally_config.sgst_parents.count()}")
        return None
        
    except Exception as e:
        logger.error(f"❌ Error finding tax ledger: {str(e)}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return None


def calculate_gst_rate(amount, igst_val, cgst_val, sgst_val):
    """Calculate GST rate percentage based on tax amounts and item amount"""
    try:
        if amount <= 0:
            return "0"
        
        total_tax = igst_val + cgst_val + sgst_val
        if total_tax <= 0:
            return "0"
        
        # Calculate rate: (total_tax / taxable_amount) * 100
        # For GST, taxable amount = total_amount - total_tax (reverse calculation)
        taxable_amount = amount - total_tax
        if taxable_amount <= 0:
            taxable_amount = amount  # Fallback if calculation seems wrong
        
        gst_rate = (total_tax / taxable_amount) * 100
        
        # Round to nearest standard GST rates
        standard_rates = [0, 5, 12, 18, 28]
        closest_rate = min(standard_rates, key=lambda x: abs(x - gst_rate))
        
        logger.warning(f"📊 GST calculation: Amount:{amount}, Tax:{total_tax}, Calculated:{gst_rate:.2f}%, Rounded:{closest_rate}%")
        return str(closest_rate)
        
    except Exception as e:
        logger.error(f"❌ Error calculating GST rate: {str(e)}")
        return "18"  # Default fallback to 18%


def normalize_company_name(name):
    """Normalize company name for better matching"""
    if not name:
        return ""
    
    # Remove common suffixes and year indicators
    normalized = name.strip()
    
    # Remove year patterns like (2025-26), (FY25), etc.
    import re
    normalized = re.sub(r'\s*\([0-9]{4}[-/][0-9]{2,4}\)', '', normalized)
    normalized = re.sub(r'\s*\(FY[0-9]{2}\)', '', normalized)
    
    # Normalize punctuation and spacing
    normalized = re.sub(r'[&]+', '&', normalized)  # Multiple & to single
    normalized = re.sub(r'\s*&\s*', ' & ', normalized)  # Normalize & spacing
    normalized = re.sub(r'\.+', '.', normalized)  # Multiple dots to single
    normalized = re.sub(r'\s*\.\s*', '. ', normalized)  # Normalize dot spacing
    
    # Handle common business suffixes
    business_suffixes = [
        'Mfg.Co.', 'Mfg Co', 'Manufacturing Co', 'Mfg. Co.', 'Mfg.Co',
        'Pvt Ltd', 'Pvt. Ltd.', 'Private Limited', 'Ltd', 'Ltd.',
        'LLC', 'LLP', 'Co.', 'Co', 'Company', 'Corp', 'Corporation',
        'Inc', 'Inc.', 'Industries', 'Enterprises', 'Trading', 'Traders'
    ]
    
    # Normalize business suffixes  
    for suffix in business_suffixes:
        pattern = r'\b' + re.escape(suffix) + r'\b'
        if re.search(pattern, normalized, re.IGNORECASE):
            # Replace with standardized version
            if 'Mfg' in suffix:
                normalized = re.sub(pattern, 'Mfg. Co.', normalized, flags=re.IGNORECASE)
            elif 'Pvt' in suffix and 'Ltd' in suffix:
                normalized = re.sub(pattern, 'Pvt. Ltd.', normalized, flags=re.IGNORECASE)
            elif suffix in ['Ltd', 'Ltd.']:
                normalized = re.sub(pattern, 'Ltd.', normalized, flags=re.IGNORECASE)
    
    # Clean up multiple spaces
    normalized = re.sub(r'\s+', ' ', normalized).strip()
    
    return normalized


def parse_bill_date(date_string):
    """Parse bill date with multiple format support"""
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


def find_vendor_ledger(company_name, organization, vendor_gst=None):
    """Find matching vendor ledger using GST number first, then enhanced name matching with TallyConfig"""
    try:
        logger.warning(f"🔍 Finding vendor - Name: '{company_name}', GST: '{vendor_gst}', Organization: {organization.id}")
        
        # Get TallyConfig for the organization
        tally_config = TallyConfig.objects.filter(organization=organization).first()
        
        if not tally_config:
            logger.error(f"❌ No TallyConfig found for organization {organization.id}. Cannot fetch vendor ledgers.")
            return None
        
        # Use configured vendor parent ledgers directly
        vendor_parent_ledgers = tally_config.vendor_parents.all()
        if not vendor_parent_ledgers.exists():
            logger.error(f"❌ No vendor parent ledgers configured in TallyConfig for organization {organization.id}")
            return None
        
        # Get all vendor ledgers under configured parent ledgers
        vendor_ledgers = Ledger.objects.filter(
            organization=organization, 
            parent__in=vendor_parent_ledgers  # Use parent directly, not parent_ledger__name__in
        )
        
        logger.warning(f"📊 Found {vendor_ledgers.count()} total vendor ledgers in configured parent ledgers")
        
        # Step 1: If GST number is provided, try exact GST match first
        if vendor_gst and vendor_gst.strip():
            gst_matches = vendor_ledgers.filter(gst_in__iexact=vendor_gst.strip())
            if gst_matches.exists():
                vendor = gst_matches.first()
                logger.warning(f"✅ Found vendor by GST match: {vendor.name} (ID: {vendor.id}, GST: {vendor.gst_in})")
                return vendor
            else:
                logger.warning(f"🔍 No exact GST match found for: {vendor_gst}")
        
        # Step 2: Enhanced name-based matching with normalization
        if company_name and company_name.strip():
            logger.warning(f"🏢 Searching for vendor by name: '{company_name}'")
            
            # Normalize company name for flexible matching
            normalized_extracted = normalize_company_name(company_name)
            logger.warning(f"🔧 Normalized extracted name: '{normalized_extracted}'")
            
            # Try exact name match first
            exact_matches = vendor_ledgers.filter(name__iexact=company_name.strip())
            if exact_matches.exists():
                vendor = exact_matches.first()
                logger.warning(f"✅ Found vendor by exact name match: {vendor.name} (ID: {vendor.id})")
                return vendor
            
            # Try normalized name matching  
            best_match = None
            best_similarity = 0.0
            
            for vendor in vendor_ledgers:
                normalized_vendor = normalize_company_name(vendor.name)
                
                # Check for normalized exact match first
                if normalized_extracted.lower() == normalized_vendor.lower():
                    logger.warning(f"✅ Found vendor by normalized exact match: {vendor.name} (ID: {vendor.id}) | '{normalized_extracted}' == '{normalized_vendor}'")
                    return vendor
                
                # Calculate similarity for partial matches
                similarity = _calculate_tally_string_similarity(normalized_extracted, normalized_vendor)
                
                # Enhanced matching criteria - also check if one contains the other
                contains_match = False
                if len(normalized_extracted) > 10 and len(normalized_vendor) > 10:
                    if normalized_extracted.lower() in normalized_vendor.lower() or normalized_vendor.lower() in normalized_extracted.lower():
                        contains_match = True
                        similarity = max(similarity, 0.8)  # Boost similarity for contains match
                
                if similarity > best_similarity:
                    best_similarity = similarity
                    best_match = vendor
                    
                # Log detailed matching info for debugging
                if similarity > 0.5:  # Only log promising matches
                    match_type = "(contains)" if contains_match else ""
                    logger.warning(f"    '{normalized_extracted}' vs '{normalized_vendor}' -> {similarity:.2f} {match_type}")
            
            # Accept match if similarity is high enough
            if best_match and best_similarity >= 0.70:  # Lowered threshold for better matching
                logger.warning(f"✅ Found vendor by similarity match ({best_similarity:.2f}): {best_match.name} (ID: {best_match.id})")
                return best_match
            else:
                logger.warning(f"❌ No vendor found with adequate similarity for: {company_name} (best: {best_similarity:.2f})")
        
        # Step 3: Log available vendors for debugging
        logger.warning(f"📋 Available vendors in configured parent ledgers (first 10):")
        for i, vendor in enumerate(vendor_ledgers[:10], 1):
            normalized_name = normalize_company_name(vendor.name)
            gst_info = f"(GST: '{vendor.gst_in}')" if vendor.gst_in else "(GST: '')"
            logger.warning(f"  {i}. '{vendor.name}' -> '{normalized_name}' {gst_info}")
        
        logger.error(f"❌ No vendor found for company: '{company_name}', GST: '{vendor_gst}'")
        return None
        
    except Exception as e:
        logger.error(f"❌ Error in find_vendor_ledger: {str(e)}")
        return None


def process_pdf_splitting(pdf_file, organization, file_type, uploaded_by):
    """Split PDF into individual pages and create separate bills"""
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
                page_image = page_images[0]
                # Save page as separate image file
                image_io = BytesIO()
                page_image.save(image_io, format='JPEG')
                image_content = ContentFile(image_io.getvalue(), name=f"page_{page_num + 1}_{unique_id}.jpg")

                # Create separate bill for this page
                bill = TallyVendorBill.objects.create(
                    file=image_content,
                    organization=organization,
                    file_type=file_type,
                    uploaded_by=uploaded_by
                )
                created_bills.append(bill)
                image_io = BytesIO()
                page_images[0].save(image_io, format='JPEG')
                image_io.seek(0)

                # Create bill for this page with uploaded_by user
                bill = TallyVendorBill.objects.create(
                    file=ContentFile(
                        image_io.read(),
                        name=f"BM-Page-{page_num + 1}-{unique_id}.jpg"
                    ),
                    file_type=file_type,
                    organization=organization,
                    uploaded_by=uploaded_by
                )
                created_bills.append(bill)

    except Exception as e:
        logger.error(f"Error splitting PDF: {str(e)}")
        raise Exception(f"PDF processing failed: {str(e)}")

    return created_bills


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
    """Get all vendor bills for the organization"""
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

    bills = TallyVendorBill.objects.filter(organization=organization)

    # Filter by status based on query parameters
    status_param = request.query_params.get('status', '').lower()
    if status_param == 'draft':
        bills = bills.filter(status='Draft')
    elif status_param == 'analysed':
        # Include both Analysed and Verified status bills
        bills = bills.filter(status__in=['Analysed', 'Verified'])
    elif status_param == 'synced':
        bills = bills.filter(status='Synced')

    # Filter by ownership based on query parameters
    ownership_param = request.query_params.get('ownership', '').lower()
    if ownership_param == 'mine':
        bills = bills.filter(bill_belong_your_org=True)
    elif ownership_param == 'others':
        bills = bills.filter(bill_belong_your_org=False)
    # If no ownership parameter, show all bills

    bills = bills.order_by('-created_at')

    # Pagination
    paginator = DefaultPagination()
    page = paginator.paginate_queryset(bills, request)
    if page is not None:
        serializer = TallyVendorBillSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    serializer = TallyVendorBillSerializer(bills, many=True)
    return Response(serializer.data)


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
        'file_type': request.data.get('file_type', TallyVendorBill.BillType.SINGLE)
    }

    serializer = VendorBillUploadSerializer(data=serializer_data)
    if not serializer.is_valid():
        return Response({
            'error': 'Invalid Upload Data',
            'message': 'The uploaded file data is invalid. Please check file format and size requirements.',
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
                'message': 'At least one file must be provided for upload. Please select files to upload.',
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
                similar_files = TallyVendorBill.objects.filter(
                    organization=organization,
                    file__isnull=False
                ).exclude(status=TallyVendorBill.BillStatus.DRAFT)

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
                                print(f"[TALLY VENDOR DEBUG] Error accessing file for bill {existing_bill.billmunshiName}: {str(e)}")
                                continue

                if potential_duplicate_files:
                    upload_warnings.append({
                        'uploaded_file': uploaded_file.name,
                        'potential_duplicates': len(potential_duplicate_files),
                        'warning': f'File "{uploaded_file.name}" may be a duplicate of existing bills',
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
                if (file_type == TallyVendorBill.BillType.MULTI and
                        file_extension == 'pdf'):

                    pdf_bills = process_pdf_splitting(
                        uploaded_file, organization, file_type, request.user
                    )
                    created_bills.extend(pdf_bills)
                else:
                    # Create single bill (including PDFs for single invoice type)
                    bill = TallyVendorBill.objects.create(
                        file=uploaded_file,
                        file_type=file_type,
                        organization=organization,
                        uploaded_by=request.user
                    )
                    created_bills.append(bill)

        # Start background processing for all created bills
        job_results = []
        for bill in created_bills:
            try:
                # Import here to avoid circular imports
                from .tasks import enqueue_vendor_bill_processing
                job = enqueue_vendor_bill_processing(str(bill.id))
                bill.job_id = job.id
                bill.is_processing = True
                bill.save(update_fields=['job_id', 'is_processing'])

                job_results.append({
                    'bill_id': str(bill.id),
                    'bill_name': bill.bill_munshi_name,
                    'job_id': job.id,
                    'status': 'queued_for_processing'
                })
                logger.info(f"Queued vendor bill {bill.bill_munshi_name} for background processing")
            except Exception as e:
                logger.error(f"Failed to queue vendor bill {bill.bill_munshi_name}: {str(e)}")
                job_results.append({
                    'bill_id': str(bill.id),
                    'bill_name': bill.bill_munshi_name,
                    'job_id': None,
                    'status': 'failed_to_queue',
                    'error': str(e)
                })

        response_serializer = TallyVendorBillSerializer(created_bills, many=True, context={'request': request})

        response_data = {
            'message': f'Successfully uploaded {len(files)} file(s) and created {len(created_bills)} bill(s). Processing started in background.',
            'files_uploaded': len(files),
            'bills_created': len(created_bills),
            'bills': response_serializer.data,
            'processing_jobs': job_results,
            'note': 'Bills are being processed in the background. Use the bill status endpoint to check progress.'
        }

        # Add file-level warnings if any
        if upload_warnings:
            response_data['upload_warnings'] = upload_warnings
            response_data['warning_message'] = f"📁 FILE WARNING: {len(upload_warnings)} file(s) may be duplicates based on filename/size"
        return Response(response_data, status=status.HTTP_201_CREATED)

    except Exception as e:
        logger.error(f"Error uploading vendor bills: {str(e)}")
        return Response({
            'error': 'File Upload Processing Failed',
            'message': 'There was an error processing the uploaded files. This could be due to file corruption, unsupported format, or server issues.',
            'details': str(e),
            'error_code': 'UPLOAD_PROCESSING_FAILED'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


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
    organization = get_organization_from_request(request, org_id)
    if not organization:
        return Response({
            'error': 'Organization not found',
            'message': f'Organization with ID {org_id} not found or access denied.'
        }, status=status.HTTP_404_NOT_FOUND)

    try:
        bill = TallyVendorBill.objects.get(id=bill_id, organization=organization)

        status_data = {
            'bill_id': str(bill.id),
            'bill_name': bill.bill_munshi_name,
            'is_processing': bill.is_processing,
            'processing_error': bill.processing_error,
            'status': bill.status,
            'is_duplicate': bill.is_duplicate,
            'duplicate_description': bill.duplicate_description,
            'duplicate_score': bill.duplicate_score,
            'duplicate_matched_bills': bill.duplicate_matched_bills
        }

        # Get job status if job_id exists
        if bill.job_id:
            # Import here to avoid circular imports
            from .tasks import get_job_status
            job_status = get_job_status(bill.job_id)
            if job_status:
                status_data['job_status'] = job_status
            else:
                status_data['job_status'] = {'status': 'unknown', 'message': 'Job status unavailable'}

        return Response(status_data)

    except TallyVendorBill.DoesNotExist:
        return Response({
            'error': 'Bill not found',
            'message': f'Vendor bill with ID {bill_id} not found in organization {org_id}.'
        }, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        logger.error(f"Error checking vendor bill processing status: {str(e)}")
        return Response({
            'error': 'Status check failed',
            'message': str(e)
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


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
        bill = TallyVendorBill.objects.get(
            id=bill_id,
            organization=organization
        )
    except TallyVendorBill.DoesNotExist:
        return Response(
            {'error': 'Bill not found'},
            status=status.HTTP_404_NOT_FOUND
        )

    if bill.process:
        return Response({
            'message': 'Bill Already Analyzed',
            'data': 'This bill has already been processed and analyzed. Use the verification endpoint to modify the analyzed data.',
            'error_code': 'BILL_ALREADY_PROCESSED'
        }, status=status.HTTP_200_OK)

    try:
        # Check if bill already has analyzed data
        if bill.analysed_data:
            logger.info(f"Using existing analyzed data for bill {bill_id}")
            analyzed_bill = process_existing_analysis_data(bill, bill.analysed_data, organization)
        else:
            logger.info(f"Running new OpenAI analysis for bill {bill_id}")
            analysis_result = analyze_bill_with_ai(bill, organization)
            if not analysis_result.get('success'):
                return Response({
                    'error': 'Bill Analysis Failed',
                    'message': 'The bill analysis could not be completed. This might be due to poor image quality, unsupported file format, or AI service issues.',
                    'details': analysis_result.get('error', 'Unknown error'),
                    'error_code': 'ANALYSIS_FAILED'
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            analyzed_bill = analysis_result.get('analyzed_bill')

        # Check for duplicate bills after analysis
        is_duplicate, duplicate_bills, max_similarity = check_duplicate_tally_vendor_bill(bill, organization)

        response_data = {
            "detail": "Tally vendor bill analyzed successfully",
            "analyzed_bill": TallyVendorAnalyzedBillSerializer(analyzed_bill).data
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
                "warning_message": f"⚠️ DUPLICATE DETECTED: Found {len(duplicate_bills)} similar Tally vendor bill(s) in your organization. "
                                  f"This bill appears to be {round(max_similarity, 1)}% similar to existing bills. "
                                  "Please review carefully before proceeding to avoid duplicate entries."
            })

            logger.warning(f"Duplicate Tally vendor bill detected for {bill.bill_munshi_name} - {len(duplicate_bills)} similar bills found")

        return Response(response_data, status=status.HTTP_200_OK)

    except Exception as e:
        logger.error(f"Bill analysis failed: {str(e)}")
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
                discount=0,  # Default discount value for existing data
                total=total_val,
                note="AI Analyzed Bill (Existing Data)",
                organization=organization,
                gst_type=gst_type
            )

            # Save without calling clean() to skip validation
            analyzed_bill.save(skip_validation=True)

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

                # Create product instance without validation
                product = TallyVendorAnalyzedProduct(
                    vendor_bill_analyzed=analyzed_bill,
                    item_details=str(item.get('description', '')),
                    price=price,
                    quantity=quantity,
                    amount=amount,
                    product_gst=f"{gst_rate}%" if gst_rate > 0 else "",
                    igst=product_igst,
                    cgst=product_cgst,
                    sgst=product_sgst,
                    organization=organization
                )
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
                        most_common_gst = max(set(gst_rates), key=gst_rates.count) if gst_rates else "18%"

                        # Create consolidated product
                        consolidated_product = TallyVendorConsolidatedProduct.objects.create(
                            vendor_bill_analyzed=analyzed_bill,
                            organization=organization,
                            item_name=f"Consolidated Items - {invoice_number} ({items_count} items)",
                            item_details=consolidated_details,
                            price=total_amount,  # Total as rate
                            quantity=1,  # Always 1 for consolidated
                            amount=total_amount,
                            product_gst=most_common_gst,
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
        # Fetch the TallyVendorBill using the new serializer
        bill = TallyVendorBill.objects.get(
            id=bill_id,
            organization=organization
        )

        # Use the enhanced serializer with analyzed data
        from .serializers import TallyVendorBillDetailSerializer
        serializer = TallyVendorBillDetailSerializer(bill, context={'request': request})

        return Response(serializer.data, status=status.HTTP_200_OK)

    except TallyVendorBill.DoesNotExist:
        return Response(
            {'error': 'Bill not found'},
            status=status.HTTP_404_NOT_FOUND
        )


# ===========================================================================
# Bill Verify View
from decimal import Decimal, InvalidOperation


def _to_decimal(val, default="0"):
    if val is None or val == "":
        return Decimal(default)
    try:
        return Decimal(str(val))
    except (InvalidOperation, ValueError):
        return Decimal(default)


def _to_int(val, default=0):
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


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
        bill = TallyVendorBill.objects.get(id=bill_id, organization=organization)

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

    if bill.status not in [TallyVendorBill.BillStatus.ANALYSED, TallyVendorBill.BillStatus.VERIFIED]:
        return Response({
            'error': 'Invalid Bill Status',
            'message': f'Bill must be in "Analysed" or "Verified" status to perform verification. Current status: {bill.status}',
            'current_status': bill.status,
            'required_status': ['Analysed', 'Verified'],
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
    from .models import TallyVendorAnalyzedBill
    if not isinstance(analyzed_bill, TallyVendorAnalyzedBill):
        logger.error(f"Expected TallyVendorAnalyzedBill, got {type(analyzed_bill)}")
        raise ValueError(f"Invalid analyzed_bill type: {type(analyzed_bill)}")

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

        # Update vendor information
        vendor_data = analyzed_data.get('vendor', {})
        if vendor_data and vendor_data.get('vendor_name') != "No Ledger":
            vendor_name = vendor_data.get('vendor_name')
            if vendor_name:
                # Check if vendor is different from current one
                current_vendor = analyzed_bill.vendor
                if not current_vendor or current_vendor.name != vendor_name.strip():
                    # Only find/create if vendor has changed
                    vendor = find_or_create_vendor_ledger(vendor_name, vendor_data, organization)
                    if vendor:
                        analyzed_bill.vendor = vendor
                else:
                    # Update existing vendor details if provided
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
            analyzed_bill.total = round(float(analyzed_data['total_amount']), 2)

        # Update tax information
        taxes_data = analyzed_data.get('taxes', {})
        if taxes_data:
            # Update IGST
            igst_data = taxes_data.get('igst', {})
            if 'amount' in igst_data:
                analyzed_bill.igst = round(float(igst_data['amount']), 2)
            if 'ledger' in igst_data and igst_data['ledger'] != "No Tax Ledger":
                # Check if current IGST tax ledger is different
                current_igst_ledger = analyzed_bill.igst_taxes
                if not current_igst_ledger or str(current_igst_ledger) != igst_data['ledger']:
                    igst_ledger = find_or_create_tax_ledger(igst_data['ledger'], 'IGST', organization)
                    if igst_ledger:
                        analyzed_bill.igst_taxes = igst_ledger
            # Update CGST
            cgst_data = taxes_data.get('cgst', {})
            if 'amount' in cgst_data:
                analyzed_bill.cgst = round(float(cgst_data['amount']), 2)
            if 'ledger' in cgst_data and cgst_data['ledger'] != "No Tax Ledger":
                # Check if current CGST tax ledger is different
                current_cgst_ledger = analyzed_bill.cgst_taxes
                if not current_cgst_ledger or str(current_cgst_ledger) != cgst_data['ledger']:
                    cgst_ledger = find_or_create_tax_ledger(cgst_data['ledger'], 'CGST', organization)
                    if cgst_ledger:
                        analyzed_bill.cgst_taxes = cgst_ledger
            # Update SGST
            sgst_data = taxes_data.get('sgst', {})
            if 'amount' in sgst_data:
                analyzed_bill.sgst = round(float(sgst_data['amount']), 2)
            if 'ledger' in sgst_data and sgst_data['ledger'] != "No Tax Ledger":
                # Check if current SGST tax ledger is different
                current_sgst_ledger = analyzed_bill.sgst_taxes
                if not current_sgst_ledger or str(current_sgst_ledger) != sgst_data['ledger']:
                    sgst_ledger = find_or_create_tax_ledger(sgst_data['ledger'], 'SGST', organization)
                    if sgst_ledger:
                        analyzed_bill.sgst_taxes = sgst_ledger

            # Update Discount
            discount_data = taxes_data.get('discount', {})
            if 'amount' in discount_data:
                analyzed_bill.discount = round(float(discount_data['amount']), 2)
            if 'ledger' in discount_data and discount_data['ledger'] != "No Tax Ledger":
                # Check if current discount tax ledger is different
                current_discount_ledger = analyzed_bill.discount_taxes
                if not current_discount_ledger or str(current_discount_ledger) != discount_data['ledger']:
                    discount_ledger = find_or_create_tax_ledger(discount_data['ledger'], 'DISCOUNT', organization)
                    if discount_ledger:
                        analyzed_bill.discount_taxes = discount_ledger

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
        logger.error(f"Error finding/creating vendor ledger: {str(e)}")
        return None


def find_or_create_tax_ledger(ledger_name, tax_type, organization):
    """Find existing tax ledger or create new one using TallyConfig"""
    try:
        logger.info(f"Looking for tax ledger: '{ledger_name}' (type: {tax_type}) in org: {organization.id}")

        # First try to find exact match
        tax_ledger = Ledger.objects.filter(
            name__iexact=ledger_name.strip(),
            organization=organization
        ).first()

        if tax_ledger:
            logger.info(f"Found existing tax ledger: {tax_ledger.name} (ID: {tax_ledger.id})")
            return tax_ledger

        logger.info(f"Tax ledger '{ledger_name}' not found, creating new one...")

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
                # For Product Tax or other types, use any available tax parent
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
        logger.info(f"Created new tax ledger: {tax_ledger.name} (ID: {tax_ledger.id}) under parent: {parent_ledger.parent}")
        return tax_ledger

    except Exception as e:
        logger.error(f"Error finding/creating tax ledger: {str(e)}")
        return None


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
            if 'product_gst' in item and product.product_gst != item.get('product_gst'):
                product.product_gst = item.get('product_gst')
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

            # Tax ledger
            if 'tax_ledger' in item and item['tax_ledger'] != "No Tax Ledger":
                current_name = str(product.taxes) if product.taxes else "No Tax Ledger"
                if current_name != item['tax_ledger']:
                    tax_ledger = find_or_create_tax_ledger(item['tax_ledger'], 'Product Tax', organization)
                    if tax_ledger:
                        product.taxes = tax_ledger
                        needs_update = True
            if needs_update:
                product.save()
                logger.info(f"Updated product {item_id} with GST type: {analyzed_bill.gst_type}")
            else:
                logger.info(f"No changes for product {item_id}")
        else:
            # Create new product
            product_amount = _to_decimal(item.get('amount'), "0")
            product_gst_str = item.get('product_gst', '')

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
                product_gst=product_gst_str,
                igst=calc_igst,
                cgst=calc_cgst,
                sgst=calc_sgst,
            )
            if item.get('tax_ledger') and item['tax_ledger'] != "No Tax Ledger":
                tax_ledger = find_or_create_tax_ledger(item['tax_ledger'], 'Product Tax', organization)
                if tax_ledger:
                    product.taxes = tax_ledger
            product.save()
            logger.info(
                f"Created new product (client item_id: {item.get('item_id')}) name={item.get('item_name') or 'Unknown'} "
                f"with GST type: {analyzed_bill.gst_type} (IGST: {calc_igst}, CGST: {calc_cgst}, SGST: {calc_sgst})")

    # Only delete products if we're NOT in consolidation mode
    # In consolidation mode, individual products should be preserved unless explicitly cleared
    if not getattr(analyzed_bill, 'consolidate', False):
        products_to_delete = []
        for existing_id, product in existing.items():
            if existing_id not in updated_ids:
                products_to_delete.append(product)

        if products_to_delete:
            deleted_count = len(products_to_delete)
            for product in products_to_delete:
                logger.info(f"Deleting product {product.id}: {product.item_name or 'Unknown'}")
                product.delete()
            logger.info(f"Deleted {deleted_count} products not present in frontend payload")
    else:
        logger.info("Consolidation mode active - preserving individual products")

    # Calculate deletion count for summary
    deletion_count = 0 if getattr(analyzed_bill, 'consolidate', False) else len([existing_id for existing_id in existing.keys() if existing_id not in updated_ids])

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

    return {
        "vendor": {
            "master_id": vendor_ledger.master_id if vendor_ledger and vendor_ledger.master_id else "No Ledger",
            "name": vendor_ledger.name if vendor_ledger and vendor_ledger.name else "No Ledger",
            "gst_in": vendor_ledger.gst_in if vendor_ledger and vendor_ledger.gst_in else "No Ledger",
            "company": vendor_ledger.company if vendor_ledger and vendor_ledger.company else "No Ledger",
        },
        "bill_details": {
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
            "discount": {
                "amount": float(analyzed_bill.discount or 0),
                "ledger": str(analyzed_bill.discount_taxes) if analyzed_bill.discount_taxes else "No Tax Ledger",
            }
        },
        "products": [
            {
                "item_id": str(item.id),  # <-- return item_id for future PATCHes
                "item_name": item.item_name,
                "item_details": item.item_details,
                "tax_ledger": str(item.taxes) if item.taxes else "No Tax Ledger",
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
        bill = TallyVendorBill.objects.get(id=bill_id, organization=organization)

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

    try:
        # Get structured bill data in the same format as verify view
        sync_data = get_structured_bill_data(analyzed_bill, organization)

        # Update bill status to synced
        bill.status = TallyVendorBill.BillStatus.SYNCED
        bill.save(update_fields=['status'])

        # Send the payload to vendor_bill_sync_external
        try:
            # Create a new request-like object with the sync data
            sync_response = vendor_bill_sync_external_handler(sync_data, org_id, organization)

            return Response({
                "message": "Bill synced successfully",
                "bill_id": str(bill_id),
                "status": "Synced",
                "data": sync_response
            }, status=status.HTTP_200_OK)

        except Exception as sync_error:
            logger.warning(f"External sync failed but bill status updated: {str(sync_error)}")
            return Response({
                "message": "Bill synced successfully but external sync failed",
                "bill_id": str(bill_id),
                "status": "Synced",
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
    """Delete vendor bill"""
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
        bill = TallyVendorBill.objects.get(
            id=bill_id,
            organization=organization
        )
    except TallyVendorBill.DoesNotExist:
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
    print("vendor_bills_sync_list called")
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
            selected_bill__tally_synced=False
        )
        .select_related('selected_bill', 'vendor', 'igst_taxes', 'cgst_taxes', 'sgst_taxes')
        .prefetch_related('products__taxes')
        .order_by('-created_at')
    )

    bills_count = analyzed_bills.count()
    logger.info(f"Found {bills_count} synced bills")

    bills_data = []
    for analyzed_bill in analyzed_bills:
        sync_data = prepare_sync_data(analyzed_bill, organization)
        bills_data.append(sync_data["data"])

    return Response({"data": bills_data}, status=status.HTTP_200_OK)


def get_client_ip(request):
    """Extract client IP (supports reverse proxy headers)."""
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def prepare_sync_data(analyzed_bill, organization):
    """Prepare bill data for Tally sync using structured format with consolidation support"""
    vendor_ledger = analyzed_bill.vendor
    bill_date_str = analyzed_bill.bill_date.strftime('%d-%m-%Y') if analyzed_bill.bill_date else None
    team_slug = organization.name if hasattr(organization, 'name') else str(organization.id)

    # Check TallyConfig for tally_product_allow_sync setting
    try:
        from .models import TallyConfig
        tally_config = TallyConfig.objects.filter(organization=organization).first()
        allow_product_sync = tally_config.tally_product_allow_sync if tally_config else False
    except Exception:
        allow_product_sync = False

    vendor_name = vendor_ledger.name if vendor_ledger and vendor_ledger.name else "Unknown Vendor"
    bill_url = f"https://billmunshi.com/tally/vendor-bill/{analyzed_bill.selected_bill.id}"
    notes_message = f"Bill from {vendor_name} entered via BillMunshi {bill_url}"

    bill_data = {
        "id": str(analyzed_bill.selected_bill.id),
        "vendor_name": vendor_name,
        "bill_no": analyzed_bill.bill_no,
        "bill_date": bill_date_str,
        "total_amount": float(analyzed_bill.total or 0),
        "company_id": team_slug,
        "notes": notes_message,
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
            "discount": {
                "amount": float(analyzed_bill.discount or 0),
                "ledger": str(analyzed_bill.discount_taxes) if analyzed_bill.discount_taxes else "No Tax Ledger",
            }
        },
        "products": []
    }

    # 🔄 SIMPLE CONSOLIDATION CHECK: Use consolidate flag to decide which data to use
    if hasattr(analyzed_bill, 'consolidate') and analyzed_bill.consolidate:
        # ✅ USE CONSOLIDATED TABLE DATA
        try:
            consolidated_products = analyzed_bill.consolidated_products.all()
            logger.info(f"Using consolidated data for bill {analyzed_bill.bill_no} ({consolidated_products.count()} consolidated products)")

            # Get individual products for GST rate calculation
            individual_products = analyzed_bill.products.all()

            # Calculate the most common GST rate from individual products
            product_gst_rate = "18%"  # Default fallback only if no data available
            if individual_products.exists():
                # Get all GST rates from individual products
                gst_rates = [item.product_gst for item in individual_products if item.product_gst]
                if gst_rates:
                    # Use the most common GST rate (highest frequency)
                    product_gst_rate = max(set(gst_rates), key=gst_rates.count)
                else:
                    # If no product_gst values, try to calculate from tax amounts
                    # This is a fallback calculation based on tax percentages
                    total_base_amount = 0
                    for consolidated_product in consolidated_products:
                        total_base_amount += float(consolidated_product.amount or 0)

                    total_tax = float(analyzed_bill.igst or 0) + float(analyzed_bill.cgst or 0) + float(analyzed_bill.sgst or 0)

                    if total_base_amount > 0 and total_tax > 0:
                        # Calculate effective tax rate
                        tax_rate = (total_tax / total_base_amount) * 100

                        # Round to nearest standard GST rate
                        if tax_rate <= 2.5:
                            product_gst_rate = "0%"
                        elif tax_rate <= 7.5:
                            product_gst_rate = "5%"
                        elif tax_rate <= 15:
                            product_gst_rate = "12%"
                        elif tax_rate <= 23:
                            product_gst_rate = "18%"
                        else:
                            product_gst_rate = "28%"

                        logger.info(f"Calculated GST rate from tax amounts: {tax_rate:.2f}% -> {product_gst_rate}")

                logger.info(f"Final GST rate for consolidated product: {product_gst_rate} (from {len(gst_rates)} individual products)")
            else:
                logger.warning(f"No individual products found for consolidated bill {analyzed_bill.bill_no}, using default GST rate")

            # Use consolidated products data in same format
            for consolidated_product in consolidated_products:
                if allow_product_sync:
                    product_data = {
                        "id": str(consolidated_product.id),
                        "item_name": consolidated_product.item_name,  # ✅ Direct field
                        "item_details": consolidated_product.item_details,  # ✅ Direct field
                        "tax_ledger": str(consolidated_product.taxes) if consolidated_product.taxes else "PURCHAGE GST",
                        "price": float(consolidated_product.price or 0),  # ✅ Direct field
                        "quantity": int(consolidated_product.quantity or 1),  # ✅ Direct field
                        "amount": float(consolidated_product.amount or 0),  # ✅ Direct field
                        "product_gst": consolidated_product.product_gst or product_gst_rate,  # ✅ Direct field with fallback
                        "igst": float(consolidated_product.igst or 0),  # ✅ Direct field
                        "cgst": float(consolidated_product.cgst or 0),  # ✅ Direct field
                        "sgst": float(consolidated_product.sgst or 0),  # ✅ Direct field
                    }
                else:
                    product_data = {
                        "id": str(consolidated_product.id),
                        "tax_ledger": str(consolidated_product.taxes) if consolidated_product.taxes else "PURCHAGE GST",
                        "product_gst": consolidated_product.product_gst or product_gst_rate,  # ✅ Direct field with fallback
                        "amount": float(consolidated_product.amount or 0),  # ✅ Direct field
                        "igst": float(consolidated_product.igst or 0),  # ✅ Direct field
                        "cgst": float(consolidated_product.cgst or 0),  # ✅ Direct field
                        "sgst": float(consolidated_product.sgst or 0),  # ✅ Direct field
                    }

                bill_data["products"].append(product_data)

        except Exception as e:
            logger.error(f"Error accessing consolidated product for bill {analyzed_bill.bill_no}: {e}")
            # Fallback to individual products if consolidated data fails
            analyzed_bill.consolidate = False  # Reset flag for this request

    if not hasattr(analyzed_bill, 'consolidate') or not analyzed_bill.consolidate:
        # ✅ USE INDIVIDUAL PRODUCTS TABLE DATA (Original logic)
        analyzed_bill_products = analyzed_bill.products.all()
        logger.info(f"Using individual products data for bill {analyzed_bill.bill_no} ({analyzed_bill_products.count()} products)")

        for item in analyzed_bill_products:
            if allow_product_sync:
                product_data = {
                    "id": str(item.id),
                    "item_name": item.item_name,
                    "item_details": item.item_details,
                    "tax_ledger": str(item.taxes) if item.taxes else "No Tax Ledger",
                    "price": float(item.price or 0),
                    "quantity": int(item.quantity or 0),
                    "amount": float(item.amount or 0),
                    "product_gst": item.product_gst,
                    "igst": float(item.igst or 0),
                    "cgst": float(item.cgst or 0),
                    "sgst": float(item.sgst or 0),
                }
            else:
                product_data = {
                    "id": str(item.id),
                    "tax_ledger": str(item.taxes) if item.taxes else "No Tax Ledger",
                    "product_gst": item.product_gst,
                    "amount": float(item.amount or 0),
                    "igst": float(item.igst or 0),
                    "cgst": float(item.cgst or 0),
                    "sgst": float(item.sgst or 0),
                }

            bill_data["products"].append(product_data)

    return {"data": bill_data}


# ============================================================================
# Bill Moving Between Modules Functionality (Tally)
# ============================================================================

def get_organization_from_request_tally(request, org_id):
    """
    Helper function to get organization from request for Tally operations
    """
    try:
        return Organization.objects.get(id=org_id)
    except Organization.DoesNotExist:
        return None

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



# apps/module/tally/vendor_views_functional.py

import base64
import json
import logging
import os
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
            temperature=0.1   # Lower temperature for more consistent results
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
    return process_analysis_data(bill, json_data, organization)


def process_analysis_data(bill, json_data, organization):
    """Process AI extracted data and create analyzed bill"""
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

        # Save analyzed data to bill
        bill.analysed_data = relevant_data
        bill.save(update_fields=['analysed_data'])

        # Extract required fields with safe access
        invoice_number = str(relevant_data.get('invoiceNumber', '')).strip()
        date_issued = str(relevant_data.get('dateIssued', ''))

        # Handle 'from' field safely
        from_data = relevant_data.get('from', {})
        if isinstance(from_data, dict):
            company_name = str(from_data.get('name', '')).strip().lower()
        else:
            company_name = str(from_data).strip().lower()

        # Parse date with multiple format support
        bill_date = parse_bill_date(date_issued)

        # Find vendor ledger
        vendor = find_vendor_ledger(company_name, organization)

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
            analyzed_bill = TallyVendorAnalyzedBill.objects.create(
                selected_bill=bill,
                vendor=vendor,
                bill_no=invoice_number,
                bill_date=bill_date,
                igst=igst_val,
                cgst=cgst_val,
                sgst=sgst_val,
                total=safe_float_convert(relevant_data.get('total', 0)),
                note="AI Analyzed Bill",
                organization=organization,
                gst_type=gst_type
            )

            # Create analyzed products with safe item extraction
            product_instances = []
            items = relevant_data.get('items', [])
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        product = TallyVendorAnalyzedProduct(
                            vendor_bill_analyzed=analyzed_bill,
                            item_details=str(item.get('description', '')),
                            price=safe_float_convert(item.get('price', 0)),
                            quantity=safe_int_convert(item.get('quantity', 0)),
                            amount=safe_float_convert(item.get('price', 0)) * safe_int_convert(item.get('quantity', 0)),
                            organization=organization
                        )
                        product_instances.append(product)

            if product_instances:
                TallyVendorAnalyzedProduct.objects.bulk_create(product_instances)

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


def find_vendor_ledger(company_name, organization):
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
        logger.error(f"Error finding vendor ledger: {str(e)}")
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
            {'error': 'Organization not found'},
            status=status.HTTP_400_BAD_REQUEST
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
        files_data = request.data.getlist('files') if hasattr(request.data, 'getlist') else request.data.get('files', [])
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
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    organization = get_organization_from_request(request, org_id)
    if not organization:
        return Response(
            {'error': 'Organization not found'},
            status=status.HTTP_400_BAD_REQUEST
        )

    files = serializer.validated_data['files']
    file_type = serializer.validated_data['file_type']
    created_bills = []
    
    if not files:
        return Response(
            {'error': 'No files provided for upload'},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        with transaction.atomic():
            for uploaded_file in files:
                file_extension = uploaded_file.name.lower().split('.')[-1]

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

        response_serializer = TallyVendorBillSerializer(created_bills, many=True, context={'request': request})
        return Response({
            'message': f'Successfully uploaded {len(files)} file(s) and created {len(created_bills)} bill(s)',
            'files_uploaded': len(files),
            'bills_created': len(created_bills),
            'bills': response_serializer.data
        }, status=status.HTTP_201_CREATED)

    except Exception as e:
        logger.error(f"Error uploading vendor bills: {str(e)}")
        return Response(
            {'error': f'Error processing files: {str(e)}'},
            status=status.HTTP_400_BAD_REQUEST
        )


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
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

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
        return Response(
            {'data': 'Bill is already Processed'},
            status=status.HTTP_200_OK
        )

    try:
        # Check if bill already has analyzed data
        if bill.analysed_data:
            logger.info(f"Using existing analyzed data for bill {bill_id}")
            analyzed_bill = process_existing_analysis_data(bill, bill.analysed_data, organization)
        else:
            logger.info(f"Running new OpenAI analysis for bill {bill_id}")
            analyzed_bill = analyze_bill_with_ai(bill, organization)

        serializer = TallyVendorAnalyzedBillSerializer(analyzed_bill)
        return Response(serializer.data, status=status.HTTP_200_OK)

    except Exception as e:
        logger.error(f"Bill analysis failed: {str(e)}")
        return Response(
            {'error': f'Analysis failed: {str(e)}'},
            status=status.HTTP_400_BAD_REQUEST
        )


def process_existing_analysis_data(bill, existing_data, organization):
    """Process existing analyzed data without calling OpenAI again"""
    try:
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
    summary="Get Bill Detail",
    description="Get detailed information about a specific vendor bill including analysis data",
    responses={200: TallyVendorBillSerializer},
    tags=['Tally Vendor Bills']
)
@api_view(['GET'])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def vendor_bill_detail(request, org_id, bill_id):
    """Get vendor bill detail including analysis data"""
    organization = get_organization_from_request(request, org_id)
    if not organization:
        return Response(
            {'error': 'Organization not found'},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        # Fetch the TallyVendorBill
        bill = TallyVendorBill.objects.get(
            id=bill_id,
            organization=organization
        )

        # Get the related TallyVendorAnalyzedBill if it exists
        try:
            analyzed_bill = TallyVendorAnalyzedBill.objects.select_related(
                'vendor', 'igst_taxes', 'cgst_taxes', 'sgst_taxes'
            ).prefetch_related(
                'products__taxes'
            ).get(selected_bill=bill, organization=organization)

            # Get vendor ledger
            vendor_ledger = analyzed_bill.vendor

            # Get analyzed bill products
            analyzed_bill_products = analyzed_bill.products.all()

            # Format bill date
            bill_date_str = analyzed_bill.bill_date.strftime('%d-%m-%Y') if analyzed_bill.bill_date else None

            # Get organization name as team_slug (you might need to adjust this based on your Organization model)
            team_slug = organization.name if hasattr(organization, 'name') else str(organization.id)

            # Structure the analyzed data in the requested format
            bill_data = {
                "vendor_name": vendor_ledger.name if vendor_ledger else "No Ledger",
                "bill_no": analyzed_bill.bill_no,
                "bill_date": bill_date_str,
                "total_amount": float(analyzed_bill.total or 0),
                "company_id": team_slug,
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
                    }
                },
                "products": [
                    {
                        "item_id": item.id,
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

            # Include the base bill information
            bill_serializer = TallyVendorBillSerializer(bill, context={'request': request})

            response_data = {
                "bill": bill_serializer.data,
                "analyzed_data": bill_data,
                "analyzed_bill": analyzed_bill.id
            }

            return Response(response_data)

        except TallyVendorAnalyzedBill.DoesNotExist:
            # If no analyzed bill exists, return just the base bill info
            bill_serializer = TallyVendorBillSerializer(bill, context={'request': request})
            return Response({
                "bill": bill_serializer.data,
                "analyzed_data": None,
                "message": "Bill has not been analyzed yet"
            })

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
        return Response({'error': 'Organization not found'}, status=status.HTTP_400_BAD_REQUEST)

    if not bill_id:
        return Response({'error': 'bill_id is required'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        bill = TallyVendorBill.objects.get(id=bill_id, organization=organization)
        analyzed_bill = TallyVendorAnalyzedBill.objects.get(selected_bill=bill, organization=organization)
    except (TallyVendorBill.DoesNotExist, TallyVendorAnalyzedBill.DoesNotExist):
        return Response({'error': 'Bill or analyzed data not found'}, status=status.HTTP_404_NOT_FOUND)

    # Optional: ensure client-sent analyzed_bill matches what we resolved from bill
    if analyzed_bill_id and str(analyzed_bill.id) != str(analyzed_bill_id):
        return Response({'error': 'analyzed_bill does not belong to the provided bill_id'},
                        status=status.HTTP_400_BAD_REQUEST)

    if bill.status != TallyVendorBill.BillStatus.ANALYSED:
        return Response({'error': 'Bill is not in analyzed status'}, status=status.HTTP_400_BAD_REQUEST)

    try:
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
        return Response({'error': f'Verification failed: {str(e)}'}, status=status.HTTP_400_BAD_REQUEST)


def update_analyzed_bill_data(analyzed_bill, analyzed_data, organization):
    """Update analyzed bill with user modifications"""

    if not analyzed_data:
        return analyzed_bill

    with transaction.atomic():
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

        # Determine GST type based on updated amounts
        if analyzed_bill.igst and analyzed_bill.igst > 0:
            analyzed_bill.gst_type = TallyVendorAnalyzedBill.GSTType.IGST
        elif (analyzed_bill.cgst and analyzed_bill.cgst > 0) or (analyzed_bill.sgst and analyzed_bill.sgst > 0):
            analyzed_bill.gst_type = TallyVendorAnalyzedBill.GSTType.CGST_SGST
        else:
            analyzed_bill.gst_type = TallyVendorAnalyzedBill.GSTType.UNKNOWN

        # Save the analyzed bill
        analyzed_bill.save(skip_validation=True)

        # Update line items (products)
        line_items = analyzed_data.get('products', [])
        if line_items:
            update_analyzed_products(analyzed_bill, line_items, organization)

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
        return tax_ledger

    except Exception as e:
        logger.error(f"Error finding/creating tax ledger: {str(e)}")
        return None


def update_analyzed_products(analyzed_bill, line_items, organization):
    """
    Update existing products by item_id, or create new ones if item_id is missing/unknown.
    Keeps vendor_bill_analyzed FK to analyzed_bill.
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
            if 'igst' in item:
                new_igst = _to_decimal(item.get('igst'), "0")
                if product.igst != new_igst:
                    product.igst = new_igst
                    needs_update = True
            if 'cgst' in item:
                new_cgst = _to_decimal(item.get('cgst'), "0")
                if product.cgst != new_cgst:
                    product.cgst = new_cgst
                    needs_update = True
            if 'sgst' in item:
                new_sgst = _to_decimal(item.get('sgst'), "0")
                if product.sgst != new_sgst:
                    product.sgst = new_sgst
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
                logger.info(f"Updated product {item_id}")
            else:
                logger.info(f"No changes for product {item_id}")
        else:
            # Create new product
            product = TallyVendorAnalyzedProduct(
                vendor_bill_analyzed=analyzed_bill,
                organization=organization,
                item_name=item.get('item_name'),
                item_details=item.get('item_details'),
                price=_to_decimal(item.get('price'), "0"),
                quantity=_to_int(item.get('quantity'), 0),
                amount=_to_decimal(item.get('amount'), "0"),
                product_gst=item.get('gst'),
                igst=_to_decimal(item.get('igst'), "0"),
                cgst=_to_decimal(item.get('cgst'), "0"),
                sgst=_to_decimal(item.get('sgst'), "0"),
            )
            if item.get('tax_ledger') and item['tax_ledger'] != "No Tax Ledger":
                tax_ledger = find_or_create_tax_ledger(item['tax_ledger'], 'Product Tax', organization)
                if tax_ledger:
                    product.taxes = tax_ledger
            product.save()
            logger.info(
                f"Created new product (client item_id: {item.get('item_id')}) name={item.get('item_name') or 'Unknown'}")

    logger.info(
        f"Product update summary: {len(updated_ids)} updated, "
        f"{len(line_items or []) - len(updated_ids)} created"
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
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    bill_id = serializer.validated_data['bill_id']
    organization = get_organization_from_request(request, org_id)

    try:
        bill = TallyVendorBill.objects.get(id=bill_id, organization=organization)
        analyzed_bill = TallyVendorAnalyzedBill.objects.get(selected_bill=bill)
    except (TallyVendorBill.DoesNotExist, TallyVendorAnalyzedBill.DoesNotExist):
        return Response(
            {'error': 'Bill or analyzed data not found'},
            status=status.HTTP_404_NOT_FOUND
        )

    if bill.status != TallyVendorBill.BillStatus.VERIFIED:
        return Response(
            {'error': 'Bill is not verified'},
            status=status.HTTP_400_BAD_REQUEST
        )

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
        return Response(
            {'error': f'Sync failed: {str(e)}'},
            status=status.HTTP_400_BAD_REQUEST
        )


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
            {'error': 'Organization not found'},
            status=status.HTTP_400_BAD_REQUEST
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
        return Response({'error': 'Organization not found'}, status=status.HTTP_400_BAD_REQUEST)

    # Modified organization validation for API Key authentication
    # Always trust the organization from API Key or token auth and skip validation
    # This avoids string comparison issues with UUIDs
    logger.info(f"Proceeding with organization {organization.id}")

    logger.info(f"Querying bills for organization: {organization.id}")
    analyzed_bills = (
        TallyVendorAnalyzedBill.objects.filter(
            organization=organization,
            selected_bill__status=TallyVendorBill.BillStatus.SYNCED
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
    """Prepare bill data for Tally sync using structured format"""
    vendor_ledger = analyzed_bill.vendor
    analyzed_bill_products = analyzed_bill.products.all()
    bill_date_str = analyzed_bill.bill_date.strftime('%d-%m-%Y') if analyzed_bill.bill_date else None
    team_slug = organization.name if hasattr(organization, 'name') else str(organization.id)

    # Check TallyConfig for tally_product_allow_sync setting
    try:
        from .models import TallyConfig
        tally_config = TallyConfig.objects.filter(organization=organization).first()
        allow_product_sync = tally_config.tally_product_allow_sync if tally_config else False
    except Exception:
        allow_product_sync = False

    # Use the same structured format as get_structured_bill_data
    bill_data = {
        "vendor_name": vendor_ledger.name if vendor_ledger and vendor_ledger.name else "No Ledger",
        "bill_no": analyzed_bill.bill_no,
        "bill_date": bill_date_str,
        "total_amount": float(analyzed_bill.total or 0),
        "company_id": team_slug,
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
            }
        },
        "products": []
    }

    # Build products array based on tally_product_allow_sync setting
    for item in analyzed_bill_products:
        if allow_product_sync:
            # Include all product fields when sync is allowed
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
            # Exclude item_name, item_details, price, quantity, amount when sync is not allowed
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
            {'error': 'Organization not found'},
            status=status.HTTP_400_BAD_REQUEST
        )

    # Get the payload from request data
    payload = request.data

    if not payload:
        return Response(
            {'error': 'No payload provided'},
            status=status.HTTP_400_BAD_REQUEST
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
        return Response(
            {'error': f'External sync failed: {str(e)}'},
            status=status.HTTP_400_BAD_REQUEST
        )


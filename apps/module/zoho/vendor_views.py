# apps/module/zoho/vendor_views.py

import base64
import json
import logging
import os
import random
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
)
from .serializers.common import (
    AnalysisResponseSerializer,
)
from .serializers.vendor_bills import (
    ZohoVendorBillSerializer,
    ZohoVendorBillDetailSerializer,
    VendorZohoBillSerializer,
    ZohoVendorBillUploadSerializer,
    ZohoVendorBillMultipleUploadSerializer,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Helper Functions
# ============================================================================

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

    # Parse date
    bill_date = None
    date_issued = relevant_data.get('dateIssued', '')
    if date_issued:
        try:
            bill_date = datetime.strptime(date_issued, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            logger.warning(f"Could not parse date: {date_issued}")

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
                'total': safe_numeric_string(relevant_data.get('total')),
                'igst': safe_numeric_string(relevant_data.get('igst')),
                'cgst': safe_numeric_string(relevant_data.get('cgst')),
                'sgst': safe_numeric_string(relevant_data.get('sgst')),
                'note': f"Auto-created from analysis for {company_name or 'Unknown Vendor'}"
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
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response(
            {'error': 'Organization not found'},
            status=status.HTTP_400_BAD_REQUEST
        )

    files = serializer.validated_data['files']
    file_type = serializer.validated_data['fileType']
    created_bills = []

    print(f"[VENDOR DEBUG] After serializer validation - files count: {len(files)}, fileType: {file_type}")

    if not files:
        return Response(
            {'error': 'No files provided for upload'},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        # Temporarily removing atomic transaction to debug
        # with transaction.atomic():
        print(f"[VENDOR DEBUG] Starting to process {len(files)} files")
        for i, uploaded_file in enumerate(files):
                print(f"[VENDOR DEBUG] Processing file {i+1}/{len(files)}: {uploaded_file.name}")
                file_extension = uploaded_file.name.lower().split('.')[-1]

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

        # Debug: Print all created bills
        for i, bill in enumerate(created_bills):
            print(f"[VENDOR DEBUG] Bill {i+1}: {bill.billmunshiName} (ID: {bill.id})")

        response_serializer = ZohoVendorBillSerializer(created_bills, many=True, context={'request': request})

        # Log the successful result
        logger.info(f"Successfully processed {len(files)} files and created {len(created_bills)} bills")
        for i, bill in enumerate(created_bills):
            logger.info(f"Created bill {i+1}: {bill.billmunshiName} (ID: {bill.id})")

        return Response({
            'message': f'Successfully uploaded {len(files)} file(s) and created {len(created_bills)} bill(s)',
            'files_uploaded': len(files),
            'bills_created': len(created_bills),
            'bills': response_serializer.data
        }, status=status.HTTP_201_CREATED)

    except Exception as e:
        logger.error(f"Error uploading vendor bills: {str(e)}")
        logger.error(f"Exception type: {type(e).__name__}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return Response(
            {'error': f'Error processing files: {str(e)}'},
            status=status.HTTP_400_BAD_REQUEST
        )


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

        # Get the next bill with 'Analysed' status
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
            zoho_bill = VendorZohoBill.objects.select_related('vendor', 'tds_tcs_id').prefetch_related(
                'products__chart_of_accounts',
                'products__taxes'
            ).get(selectBill=bill, organization=organization)

            # Attach zoho_bill to the bill object for the serializer
            bill.zoho_bill = zoho_bill
        except VendorZohoBill.DoesNotExist:
            # If no VendorZohoBill exists, set it to None
            bill.zoho_bill = None

        # Serialize the data with request context for full URLs
        serializer = ZohoVendorBillDetailSerializer(bill, context={'request': request})
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
            return Response(
                {"detail": "Bill must be in 'Draft' status to analyze"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Read file content
        try:
            bill.file.seek(0)
            file_content = bill.file.read()
            file_extension = bill.file.name.split('.')[-1].lower()
        except Exception as e:
            logger.error(f"Error reading bill file: {e}")
            return Response(
                {"detail": "Error reading the bill file"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Analyze with OpenAI
        analyzed_data = analyze_vendor_bill_with_openai(file_content, file_extension)

        # Update bill with analyzed data
        bill.analysed_data = analyzed_data
        bill.status = 'Analysed'
        bill.process = True
        bill.save()

        # Create Zoho bill and product objects from analysis
        create_vendor_zoho_objects_from_analysis(bill, analyzed_data, organization)

        return Response({
            "detail": "Bill analyzed successfully",
            "analyzed_data": analyzed_data
        })

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
                logger.error(f"[DEBUG] vendor_bill_verify_view - Found vendor in database: {vendor_obj.vendor_name} (ID: {vendor_obj.id})")
            except ZohoVendor.DoesNotExist:
                logger.error(f"[DEBUG] vendor_bill_verify_view - ERROR: Vendor {vendor_data} does not exist in database")
                return Response(
                    {"detail": f"Vendor with ID {vendor_data} does not exist. Please sync vendors from Zoho first."},
                    status=status.HTTP_400_BAD_REQUEST
                )
            except Exception as vendor_check_error:
                logger.error(f"[DEBUG] vendor_bill_verify_view - Error checking vendor: {vendor_check_error}")
        else:
            logger.error(f"[DEBUG] vendor_bill_verify_view - No vendor data found in payload")

        # Use the bill_id from payload if provided, otherwise use URL parameter
        bill = VendorBill.objects.get(id=payload_bill_id, organization=organization)
        logger.error(f"[DEBUG] vendor_bill_verify_view - Found VendorBill: {bill.id}, status: {bill.status}")

        if bill.status not in ['Analyzed', 'Verified']:
            return Response(
                {"detail": "Bill must be in 'Analysed' or 'Verified' status to save"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Get existing VendorZohoBill
        try:
            zoho_bill = VendorZohoBill.objects.get(selectBill=bill, organization=organization)
            logger.error(f"[DEBUG] vendor_bill_verify_view - Found existing VendorZohoBill: {zoho_bill.id}")
            logger.error(f"[DEBUG] vendor_bill_verify_view - Current vendor in zoho_bill: {zoho_bill.vendor}")
            if zoho_bill.vendor:
                logger.error(f"[DEBUG] vendor_bill_verify_view - Current vendor details: ID={zoho_bill.vendor.id}, Name={zoho_bill.vendor.vendor_name}, ContactID={zoho_bill.vendor.contactId}")
            else:
                logger.error(f"[DEBUG] vendor_bill_verify_view - No vendor currently assigned to zoho_bill")
        except VendorZohoBill.DoesNotExist:
            logger.error(f"[DEBUG] vendor_bill_verify_view - VendorZohoBill not found for bill {bill.id}")
            return Response(
                {"detail": "No analyzed vendor data found. Please analyze the bill first."},
                status=status.HTTP_400_BAD_REQUEST
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
                logger.error(f"[DEBUG] vendor_bill_verify_view - Validated data: {serializer.validated_data}")

                # Check vendor in validated data
                vendor_in_validated = serializer.validated_data.get('vendor')
                if vendor_in_validated:
                    logger.error(f"[DEBUG] vendor_bill_verify_view - Vendor in validated_data: {vendor_in_validated} (Type: {type(vendor_in_validated)})")
                else:
                    logger.error(f"[DEBUG] vendor_bill_verify_view - No vendor in validated_data")

                # Save the serializer
                updated_bill = serializer.save()
                logger.error(f"[DEBUG] vendor_bill_verify_view - Serializer saved successfully")
                logger.error(f"[DEBUG] vendor_bill_verify_view - Updated bill ID: {updated_bill.id}")
                logger.error(f"[DEBUG] vendor_bill_verify_view - Updated bill vendor after save: {updated_bill.vendor}")

                if updated_bill.vendor:
                    logger.error(f"[DEBUG] vendor_bill_verify_view - Vendor saved successfully: ID={updated_bill.vendor.id}, Name={updated_bill.vendor.vendor_name}")
                else:
                    logger.error(f"[DEBUG] vendor_bill_verify_view - WARNING: No vendor assigned after save!")
                    # Let's check if vendor data was in the original payload
                    if 'vendor' in zoho_bill_data:
                        logger.error(f"[DEBUG] vendor_bill_verify_view - ERROR: Vendor was in payload but not saved: {zoho_bill_data['vendor']}")

                logger.error(f"[DEBUG] vendor_bill_verify_view - Complete updated bill: {updated_bill}")
                # Handle products update if provided
                products_data = zoho_bill_data.get('products')
                if products_data is not None:
                    # Get existing product IDs
                    existing_products = {str(product.id): product for product in updated_bill.products.all()}

                    # Track which products to keep
                    processed_product_ids = set()

                    for product_data in products_data:
                        if not product_data.get('item_details'):  # Skip if no item_details
                            continue

                        product_id = product_data.get('id')

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

                        # Remove None values
                        product_fields = {k: v for k, v in product_fields.items() if v is not None}

                        if product_id and str(product_id) in existing_products:
                            # Update existing product
                            existing_product = existing_products[str(product_id)]
                            for field, value in product_fields.items():
                                setattr(existing_product, field, value)
                            existing_product.save()
                            processed_product_ids.add(str(product_id))
                            logger.info(f"Updated existing product {product_id}")
                        else:
                            # Create new product
                            new_product = VendorZohoProduct.objects.create(
                                zohoBill=updated_bill,
                                organization=organization,
                                **product_fields
                            )
                            processed_product_ids.add(str(new_product.id))
                            logger.info(f"Created new product {new_product.id}")

                    # Delete products that were not in the update data
                    products_to_delete = set(existing_products.keys()) - processed_product_ids
                    if products_to_delete:
                        VendorZohoProduct.objects.filter(
                            id__in=products_to_delete,
                            zohoBill=updated_bill
                        ).delete()
                        logger.info(f"Deleted {len(products_to_delete)} products not in update")

                # Update bill status
                logger.error(f"[DEBUG] vendor_bill_verify_view - Updating bill status from '{bill.status}' to 'Verified'")
                bill.status = 'Verified'
                bill.save()
                logger.error(f"[DEBUG] vendor_bill_verify_view - Bill status updated successfully to '{bill.status}'")

                # Final verification of vendor data in response
                response_data = VendorZohoBillSerializer(
                    updated_bill, 
                    context={'organization': organization}
                ).data
                logger.error(f"[DEBUG] vendor_bill_verify_view - Response vendor data: {response_data.get('vendor')}")
                logger.error(f"[DEBUG] vendor_bill_verify_view - Verification process completed successfully")

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
        raise


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
            return Response(
                {"detail": "Bill must be in 'Verified' status to sync"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Get Zoho vendor data
        try:
            zoho_bill = VendorZohoBill.objects.get(selectBill=bill, organization=organization)
        except VendorZohoBill.DoesNotExist:
            return Response(
                {"detail": "Zoho vendor data not found. Please verify the bill first."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Get Zoho credentials
        try:
            current_token = ZohoCredentials.objects.get(organization=organization)
        except ZohoCredentials.DoesNotExist:
            return Response(
                {"detail": "Zoho credentials not found"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Get products
        zoho_products = zoho_bill.products.all()
        if not zoho_products.exists():
            return Response(
                {"detail": "No products found for this bill"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Get vendor
        if not zoho_bill.vendor:
            return Response(
                {"detail": "No vendor specified for this bill"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Prepare data for Zoho API (Vendor Bill)
        bill_date_str = zoho_bill.bill_date.strftime('%Y-%m-%d') if zoho_bill.bill_date else None
        tax_choice = getattr(zoho_bill, 'is_tax', 'No')

        bill_data = {
            "vendor_id": zoho_bill.vendor.contactId,
            "bill_number": zoho_bill.bill_no,
            "gst_no": zoho_bill.vendor.gstNo,
            "date": bill_date_str,
            "line_items": []
        }

        # Add TDS/TCS if applicable
        if hasattr(zoho_bill, 'tds_tcs_id') and zoho_bill.tds_tcs_id:
            if tax_choice == 'TDS':
                bill_data['tds_tax_id'] = str(zoho_bill.tds_tcs_id.taxId)
            elif tax_choice == 'TCS':
                bill_data['tcs_tax_id'] = str(zoho_bill.tds_tcs_id.taxId)

        # Add line items from products
        for item in zoho_products:
            try:
                # Get chart of account
                if not item.chart_of_accounts:
                    logger.warning(f"No chart of account found for product {item.id}")
                    continue

                line_item = {
                    "account_id": str(item.chart_of_accounts.accountId),
                    "rate": float(item.rate) if item.rate else 0,
                    "quantity": float(item.quantity) if item.quantity else 1,
                    "discount": 0.00,
                    "itc_eligibility": getattr(item, 'itc_eligibility', 'eligible')
                }

                # Add tax information
                if hasattr(item, 'reverse_charge_tax_id') and item.reverse_charge_tax_id and item.taxes:
                    line_item['reverse_charge_tax_id'] = item.taxes.taxId
                elif item.taxes:
                    line_item['tax_id'] = item.taxes.taxId

                bill_data["line_items"].append(line_item)

            except Exception as e:
                logger.error(f"Error processing product {item.id}: {str(e)}")
                continue

        if not bill_data["line_items"]:
            return Response(
                {"detail": "No valid line items found for syncing"},
                status=status.HTTP_400_BAD_REQUEST
            )

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
                error_message = response_json.get("message", "Failed to send data to Zoho")
                logger.error(f"Zoho sync failed: {response.status_code} - {error_message}")
                return Response(
                    {"detail": error_message},
                    status=status.HTTP_400_BAD_REQUEST
                )

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















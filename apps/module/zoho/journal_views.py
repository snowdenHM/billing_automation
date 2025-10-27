# apps/module/zoho/journal_views.py

import base64
import json
import logging
import os
from datetime import datetime
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
    JournalBill,
    JournalZohoBill,
    JournalZohoProduct,
)
from .serializers.common import (
    AnalysisResponseSerializer,
)
from .serializers.journal_bills import (
    ZohoJournalBillSerializer,
    ZohoJournalBillDetailSerializer,
    JournalZohoBillSerializer,
    ZohoJournalBillMultipleUploadSerializer,
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


def get_zoho_credentials(organization):
    """Get valid Zoho credentials for organization."""
    try:
        credentials = ZohoCredentials.objects.get(organization=organization)
        if not credentials.is_token_valid():
            if not credentials.refresh_token():
                raise ValueError("Unable to refresh Zoho token")
        return credentials
    except ZohoCredentials.DoesNotExist:
        raise ValueError("Zoho credentials not found for organization")


def make_zoho_api_request(credentials, endpoint, method='GET', data=None):
    """Make authenticated request to Zoho API."""
    headers = {
        'Authorization': f'Zoho-oauthtoken {credentials.accessToken}',
        'Content-Type': 'application/json'
    }

    url = f"https://www.zohoapis.in/books/v3/{endpoint}?organization_id={credentials.organisationId}"

    try:
        if method == 'GET':
            response = requests.get(url, headers=headers)
        elif method == 'POST':
            response = requests.post(url, headers=headers, json=data)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")

        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logger.error(f"Zoho API request failed: {str(e)}")
        raise


def analyze_bill_with_openai(file_content, file_extension):
    """
    Analyze journal bill content using OpenAI to extract structured data with enhanced PDF handling.
    Supports PDF, JPG, PNG file formats with robust validation and optimization.
    """
    logger.info(f"Starting enhanced journal bill analysis for file type: {file_extension}")

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

        # Enhanced prompt for Indian journal bills/receipts
        enhanced_prompt = """
        Analyze this journal bill/receipt image carefully and extract ALL visible information in JSON format.
        This appears to be an Indian business journal bill/receipt. Look for:
        
        1. Bill/Receipt Number (may be labeled as Bill No, Receipt No, Invoice No, etc.)
        2. Dates (Bill Date, Receipt Date, Transaction Date - convert to YYYY-MM-DD format)
        3. Vendor/Company details in "from" section (name and address)
        4. Customer details in "to" section (name and address) 
        5. journal items with descriptions, categories, and amounts
        6. Tax amounts (IGST, CGST, SGST - look for percentages and amounts)
        7. Total amount (may include terms like "Total", "Grand Total", "Amount Payable", "Net Amount")
        
        IMPORTANT RULES:
        - Extract EXACT text as it appears on the document
        - For numbers, remove currency symbols (₹, Rs.) and commas
        - If any field is not visible or unclear, use empty string "" or 0 for numbers
        - Look carefully at the entire document, including headers, footers, and margins
        - Pay special attention to tax sections which may be in tables or separate areas
        - For journal categories, try to identify the type of journal (travel, food, supplies, etc.)
        
        Return data in this JSON structure:
        {
            "invoiceNumber": "Bill/Receipt number as shown on document",
            "dateIssued": "Bill/Receipt date in YYYY-MM-DD format",
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
                    "description": "journal item description",
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
        logger.error(f"Error in enhanced journal bill analysis: {str(e)}")
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


def create_journal_zoho_objects_from_analysis(bill, analyzed_data, organization):
    """
    Create JournalZohoBill and JournalZohoProduct objects from analyzed data.
    """
    logger.info(f"Creating journal Zoho objects for bill {bill.id} with analyzed data: {analyzed_data}")

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

    # Create or update JournalZohoBill
    try:
        zoho_bill, created = JournalZohoBill.objects.get_or_create(
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
                'note': f"Bill from analysis for {company_name or 'Unknown Vendor'} entered via Billmunshi"
            }
        )

        if created:
            logger.info(f"Created new JournalZohoBill: {zoho_bill.id}")
        else:
            logger.info(f"Found existing JournalZohoBill: {zoho_bill.id}")
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
            logger.info(f"Updated existing JournalZohoBill: {zoho_bill.id}")

        # Delete existing products and recreate them
        existing_products_count = zoho_bill.products.count()
        if existing_products_count > 0:
            zoho_bill.products.all().delete()
            logger.info(f"Deleted {existing_products_count} existing products")

        # Create JournalZohoProduct objects for each item
        items = relevant_data.get('items', [])
        logger.info(f"Creating {len(items)} product line items")

        created_products = []
        for idx, item in enumerate(items):
            try:
                amount = item.get('price', 0) * item.get('quantity', 1)
                product = JournalZohoProduct.objects.create(
                    zohoBill=zoho_bill,
                    organization=organization,
                    item_details=item.get('description', f'Item {idx + 1}')[:200],
                    amount=safe_numeric_string(amount)
                )
                created_products.append(product)
                logger.info(f"Created product {idx + 1}: {product.item_details} - Amount: {product.amount}")
            except Exception as e:
                logger.error(f"Error creating product {idx + 1}: {str(e)}")
                continue

        logger.info(f"Successfully created {len(created_products)} products for bill {zoho_bill.id}")
        return zoho_bill

    except Exception as e:
        logger.error(f"Error creating Zoho objects for bill {bill.id}: {str(e)}")
        raise


def process_pdf_splitting_journal(pdf_file, organization, file_type, uploaded_by):
    """Split PDF into individual pages and create separate journal bills"""
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
                # Let the model generate billmunshiName automatically
                bill = JournalBill.objects.create(
                    file=ContentFile(
                        image_io.read(),
                        name=f"BM-journal-Page-{page_num + 1}-{unique_id}.jpg"
                    ),
                    fileType=file_type,
                    status='Draft',
                    organization=organization,
                    uploaded_by=uploaded_by
                )
                created_bills.append(bill)

    except Exception as e:
        logger.error(f"Error splitting journal PDF: {str(e)}")
        raise Exception(f"journal PDF processing failed: {str(e)}")

    return created_bills


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


# ============================================================================
# journal Bills API Views
# ============================================================================
# ✅
@extend_schema(
    responses=ZohoJournalBillSerializer(many=True),
    tags=["Zoho Journal Bills"],
    methods=["GET"]
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def journal_bills_list_view(request, org_id):
    """List all journal bills for the organization with pagination."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    bills = JournalBill.objects.filter(organization=organization)

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

    # Apply pagination
    paginator = DefaultPagination()
    paginated_bills = paginator.paginate_queryset(bills, request)

    if paginated_bills is not None:
        serializer = ZohoJournalBillSerializer(paginated_bills, many=True)
        return paginator.get_paginated_response(serializer.data)

    # Fallback if pagination fails
    serializer = ZohoJournalBillSerializer(bills, many=True)
    return Response({"results": serializer.data})


# ✅
@extend_schema(
    summary="Upload journal Bills",
    description="Upload single or multiple journal bill files (PDF, JPG, PNG). Supports both single file and multiple file uploads with PDF splitting for multiple invoices.",
    request=ZohoJournalBillMultipleUploadSerializer,
    responses={201: ZohoJournalBillSerializer(many=True)},
    tags=["Zoho Journal Bills"],
    methods=["POST"]
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser])
def journal_bill_upload_view(request, org_id):
    """Handle single or multiple journal bill file uploads with PDF splitting support"""
    
    # Handle both single file and multiple files seamlessly
    files_data = []
    
    # Debug logging with prints (will show in gunicorn logs)
    print(f"[journal DEBUG] Request data keys: {list(request.data.keys())}")
    print(f"[journal DEBUG] 'files' in request.data: {'files' in request.data}")
    print(f"[journal DEBUG] 'file' in request.data: {'file' in request.data}")
    
    # Check if files are provided as a list (multiple files)
    if 'files' in request.data:
        files_data = request.data.getlist('files') if hasattr(request.data, 'getlist') else request.data.get('files', [])
        # Ensure files_data is always a list
        if not isinstance(files_data, list):
            files_data = [files_data] if files_data else []
        print(f"[journal DEBUG] Found 'files' field with {len(files_data)} file(s)")
    # Check if a single file is provided
    elif 'file' in request.data:
        single_file = request.data.get('file')
        if single_file:
            files_data = [single_file]
        print(f"[journal DEBUG] Found 'file' field with {len(files_data)} file(s)")
    
    print(f"[journal DEBUG] Total files collected: {len(files_data)}")
    
    # Debug: Print details about each file
    for i, f in enumerate(files_data):
        print(f"[journal DEBUG] File {i+1}: {getattr(f, 'name', 'Unknown')} - Size: {getattr(f, 'size', 'Unknown')}")
    
    # Prepare data for serializer validation
    serializer_data = {
        'files': files_data,
        'fileType': request.data.get('fileType', 'Single Invoice/File')
    }
    
    print(f"[journal DEBUG] Serializer data: files count = {len(serializer_data['files'])}, fileType = {serializer_data['fileType']}")
    
    serializer = ZohoJournalBillMultipleUploadSerializer(data=serializer_data)
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
    
    print(f"[journal DEBUG] After serializer validation - files count: {len(files)}, fileType: {file_type}")
    
    if not files:
        return Response(
            {'error': 'No files provided for upload'},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        # Temporarily removing atomic transaction to debug
        # with transaction.atomic():
        print(f"[journal DEBUG] Starting to process {len(files)} files")
        for i, uploaded_file in enumerate(files):
                print(f"[journal DEBUG] Processing file {i+1}/{len(files)}: {uploaded_file.name}")
                file_extension = uploaded_file.name.lower().split('.')[-1]

                # Handle PDF splitting for multiple invoice files
                if file_type == 'Multiple Invoice/File' and file_extension == 'pdf':
                    print(f"[journal DEBUG] Processing as PDF split for file: {uploaded_file.name}")
                    pdf_bills = process_pdf_splitting_journal(
                        uploaded_file, organization, file_type, request.user
                    )
                    print(f"[journal DEBUG] PDF splitting created {len(pdf_bills)} bills")
                    created_bills.extend(pdf_bills)
                else:
                    # Create single bill (including PDFs for single invoice type)
                    # Let the model generate billmunshiName automatically
                    print(f"[journal DEBUG] Creating single bill for file: {uploaded_file.name}")
                    bill = JournalBill.objects.create(
                        file=uploaded_file,
                        fileType=file_type,
                        status='Draft',
                        organization=organization,
                        uploaded_by=request.user
                    )
                    print(f"[journal DEBUG] Created bill: {bill.billmunshiName} (ID: {bill.id})")
                    created_bills.append(bill)
        
        print(f"[journal DEBUG] Completed processing all files. Total bills created: {len(created_bills)}")
        
        # Debug: Print all created bills
        for i, bill in enumerate(created_bills):
            print(f"[journal DEBUG] Bill {i+1}: {bill.billmunshiName} (ID: {bill.id})")

        response_serializer = ZohoJournalBillSerializer(created_bills, many=True, context={'request': request})
        
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
        logger.error(f"Error uploading journal bills: {str(e)}")
        logger.error(f"Exception type: {type(e).__name__}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return Response({
            'detail': f'Error processing files: {str(e)}'
        }, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(
    responses=ZohoJournalBillDetailSerializer,
    tags=["Zoho Journal Bills"],
    methods=["GET"]
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def journal_bill_detail_view(request, org_id, bill_id):
    """Get journal bill details including analysis data."""
    logger.info(f"[DEBUG] journal_bill_detail_view - Starting request for org_id: {org_id}, bill_id: {bill_id}")

    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        logger.error(f"[DEBUG] journal_bill_detail_view - Organization not found: {org_id}")
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        # Fetch the JournalBill
        bill = JournalBill.objects.get(id=bill_id, organization=organization)

        # Get the next bill with 'Analysed' status
        next_bill_id = None
        analysed_bills = JournalBill.objects.filter(
            organization=organization,
            status='Analysed'
        ).exclude(id=bill_id).values_list('id', flat=True)

        if analysed_bills:
            next_bill_id = str(analysed_bills[0])  # Get the first analysed bill
            logger.info(f"Found next analysed journal bill: {next_bill_id}")
        else:
            logger.info("No analysed journal bills found for next_bill")

        # Always set next_bill on the bill object
        bill.next_bill = next_bill_id

        logger.info(f"[DEBUG] journal_bill_detail_view - Processing bill ID: {bill_id} for organization: {organization.name}")

        # Get the related JournalZohoBill if it exists
        try:
            zoho_bill = JournalZohoBill.objects.select_related('vendor').prefetch_related(
                'products__chart_of_accounts',
                'products__taxes'
            ).get(selectBill=bill, organization=organization)

            logger.info(f"[DEBUG] journal_bill_detail_view - Found JournalZohoBill: {zoho_bill.id}")
            # Attach zoho_bill to the bill object for the serializer
            bill.zoho_bill = zoho_bill
        except JournalZohoBill.DoesNotExist:
            logger.info(f"[DEBUG] journal_bill_detail_view - No JournalZohoBill found for bill: {bill_id}")
            # If no JournalZohoBill exists, set it to None
            bill.zoho_bill = None

        # Serialize the data with request context for full URLs
        serializer = ZohoJournalBillDetailSerializer(bill, context={'request': request})
        logger.info(f"[DEBUG] journal_bill_detail_view - Successfully serialized bill data")
        return Response(serializer.data)

    except JournalBill.DoesNotExist:
        logger.error(f"[DEBUG] journal_bill_detail_view - JournalBill not found: {bill_id}")
        return Response({"detail": "journal bill not found"}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        logger.error(f"[DEBUG] journal_bill_detail_view - Unexpected error: {str(e)}")
        return Response({"detail": "Internal server error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

# ✅
@extend_schema(
    responses=AnalysisResponseSerializer,
    tags=["Zoho Journal Bills"],
    methods=["POST"]
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def journal_bill_analyze_view(request, org_id, bill_id):
    """Analyze journal bill using OpenAI. Changes status from 'Draft' to 'Analyzed'."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        bill = JournalBill.objects.get(id=bill_id, organization=organization)

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
        analyzed_data = analyze_bill_with_openai(file_content, file_extension)

        # Update bill with analyzed data
        bill.analysed_data = analyzed_data
        bill.status = 'Analysed'
        bill.process = True
        bill.save()

        # Create Zoho bill and product objects from analysis
        create_journal_zoho_objects_from_analysis(bill, analyzed_data, organization)

        return Response({
            "detail": "Bill analyzed successfully",
            "analyzed_data": analyzed_data
        })

    except JournalBill.DoesNotExist:
        return Response({"detail": "journal bill not found"}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        logger.error(f"Analysis failed: {str(e)}")
        return Response(
            {"detail": f"Analysis failed: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@extend_schema(
    request=JournalZohoBillSerializer,
    responses=JournalZohoBillSerializer,
    tags=["Zoho Journal Bills"],
    methods=["POST"]
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def journal_bill_verify_view(request, org_id, bill_id):
    """Verify and update Zoho journal data. Changes status from 'Analyzed' to 'Verified'."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        # Get data from request - following the exact same pattern as vendor_views.py
        payload_bill_id = request.data.get('bill_id', bill_id)
        zoho_bill_data = request.data.get('zoho_bill', request.data)

        logger.info(f"[DEBUG] journal_bill_verify_view - Processing bill ID: {payload_bill_id}")
        logger.info(f"[DEBUG] journal_bill_verify_view - URL bill_id: {bill_id}")
        logger.info(f"[DEBUG] journal_bill_verify_view - request.data bill_id: {request.data.get('bill_id', 'Not found')}")
        logger.info(f"[DEBUG] journal_bill_verify_view - Received zoho_bill_data keys: {list(zoho_bill_data.keys()) if zoho_bill_data else 'None'}")
        logger.info(f"[DEBUG] journal_bill_verify_view - Full request.data: {request.data}")

        # Debug vendor data in the payload
        vendor_data = zoho_bill_data.get('vendor')
        if vendor_data:
            logger.info(f"[DEBUG] journal_bill_verify_view - Vendor data in payload: {vendor_data}")
            logger.info(f"[DEBUG] journal_bill_verify_view - Vendor data type: {type(vendor_data)}")

            # Validate vendor exists before proceeding
            try:
                from .models import ZohoVendor
                vendor_obj = ZohoVendor.objects.get(id=vendor_data, organization=organization)
                logger.info(f"[DEBUG] journal_bill_verify_view - Found vendor in database: {vendor_obj.companyName} (ID: {vendor_obj.id})")
            except ZohoVendor.DoesNotExist:
                logger.error(f"[DEBUG] journal_bill_verify_view - ERROR: Vendor {vendor_data} does not exist in organization {organization.name}")
                return Response(
                    {"detail": f"Vendor with ID {vendor_data} does not exist in this organization. Please sync vendors from Zoho first."},
                    status=status.HTTP_400_BAD_REQUEST
                )
            except Exception as vendor_check_error:
                logger.error(f"[DEBUG] journal_bill_verify_view - Error checking vendor: {vendor_check_error}")
        else:
            logger.info(f"[DEBUG] journal_bill_verify_view - No vendor data found in payload")

        # Use the bill_id from payload if provided, otherwise use URL parameter
        bill = JournalBill.objects.get(id=payload_bill_id, organization=organization)
        logger.info(f"[DEBUG] journal_bill_verify_view - Found JournalBill: {bill.id}, status: {bill.status}")

        if bill.status not in ['Analysed', 'Verified']:
            return Response(
                {"detail": "Bill must be in 'Analysed' or 'Verified' status to save"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Get existing JournalZohoBill
        logger.info(f"[DEBUG] journal_bill_verify_view - Attempting to find JournalZohoBill for bill: {bill.id}, org: {organization.id}")
        try:
            zoho_bill = JournalZohoBill.objects.get(selectBill=bill, organization=organization)
            logger.info(f"[DEBUG] journal_bill_verify_view - Found existing JournalZohoBill: {zoho_bill.id}")
            logger.info(f"[DEBUG] journal_bill_verify_view - Current vendor in zoho_bill: {zoho_bill.vendor}")
            if zoho_bill.vendor:
                logger.info(f"[DEBUG] journal_bill_verify_view - Current vendor details: ID={zoho_bill.vendor.id}, Name={zoho_bill.vendor.companyName}, ContactID={zoho_bill.vendor.contactId}")
            else:
                logger.info(f"[DEBUG] journal_bill_verify_view - No vendor currently assigned to zoho_bill")
        except JournalZohoBill.DoesNotExist:
            logger.error(f"[DEBUG] journal_bill_verify_view - JournalZohoBill not found for bill {bill.id}")
            return Response(
                {"detail": "No analyzed journal data found. Please analyze the bill first."},
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception as zoho_bill_error:
            logger.error(f"[DEBUG] journal_bill_verify_view - Unexpected error getting JournalZohoBill: {zoho_bill_error}")
            logger.error(f"[DEBUG] journal_bill_verify_view - Error type: {type(zoho_bill_error).__name__}")
            import traceback
            logger.error(f"[DEBUG] journal_bill_verify_view - Traceback: {traceback.format_exc()}")
            return Response(
                {"detail": f"Error retrieving journal data: {str(zoho_bill_error)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        with transaction.atomic():
            # Use partial=True for POST as we're updating existing data
            logger.info(f"[DEBUG] journal_bill_verify_view - Creating serializer with partial=True")
            logger.info(f"[DEBUG] journal_bill_verify_view - Serializer data being passed: {zoho_bill_data}")

            # Pass organization in context for proper vendor queryset scoping
            serializer = JournalZohoBillSerializer(
                zoho_bill, 
                data=zoho_bill_data, 
                partial=True,
                context={'organization': organization}
            )

            if serializer.is_valid():
                logger.info(f"[DEBUG] journal_bill_verify_view - Serializer is valid, proceeding to save")
                logger.info(f"[DEBUG] journal_bill_verify_view - Validated data: {serializer.validated_data}")

                # Check vendor in validated data
                vendor_in_validated = serializer.validated_data.get('vendor')
                if vendor_in_validated:
                    logger.info(f"[DEBUG] journal_bill_verify_view - Vendor in validated_data: {vendor_in_validated} (Type: {type(vendor_in_validated)})")
                else:
                    logger.info(f"[DEBUG] journal_bill_verify_view - No vendor in validated_data")

                # Save the serializer
                updated_bill = serializer.save()
                logger.info(f"[DEBUG] journal_bill_verify_view - Serializer saved successfully")
                logger.info(f"[DEBUG] journal_bill_verify_view - Updated bill ID: {updated_bill.id}")
                logger.info(f"[DEBUG] journal_bill_verify_view - Updated bill vendor after save: {updated_bill.vendor}")

                if updated_bill.vendor:
                    logger.info(f"[DEBUG] journal_bill_verify_view - Vendor saved successfully: ID={updated_bill.vendor.id}, Name={updated_bill.vendor.companyName}")
                else:
                    logger.info(f"[DEBUG] journal_bill_verify_view - WARNING: No vendor assigned after save!")
                    # Let's check if vendor data was in the original payload
                    if 'vendor' in zoho_bill_data:
                        logger.error(f"[DEBUG] journal_bill_verify_view - ERROR: Vendor was in payload but not saved: {zoho_bill_data['vendor']}")

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

                        # Validate taxes for this product
                        product_taxes_id = product_data.get('taxes')
                        if product_taxes_id:
                            try:
                                from .models import ZohoTaxes
                                product_taxes = ZohoTaxes.objects.get(id=product_taxes_id, organization=organization)
                                logger.info(f"[DEBUG] journal_bill_verify_view - Product taxes validated: {product_taxes.taxName}")
                            except ZohoTaxes.DoesNotExist:
                                logger.error(f"[DEBUG] journal_bill_verify_view - ERROR: Product taxes {product_taxes_id} does not exist")
                                return Response(
                                    {"detail": f"Product taxes with ID {product_taxes_id} does not exist in this organization. Please sync taxes from Zoho first."},
                                    status=status.HTTP_400_BAD_REQUEST
                                )

                        # Prepare product data for creation/update
                        product_fields = {
                            'item_details': product_data.get('item_details'),
                            'chart_of_accounts_id': product_data.get('chart_of_accounts'),
                            'taxes_id': product_data.get('taxes'),
                            'amount': product_data.get('amount'),
                            'debit_or_credit': product_data.get('debit_or_credit', 'credit'),
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
                            logger.info(f"Updated existing journal product {product_id}")
                        else:
                            # Create new product
                            new_product = JournalZohoProduct.objects.create(
                                zohoBill=updated_bill,
                                organization=organization,
                                **product_fields
                            )
                            processed_product_ids.add(str(new_product.id))
                            logger.info(f"Created new journal product {new_product.id}")

                    # Delete products that were not in the update data
                    products_to_delete = set(existing_products.keys()) - processed_product_ids
                    if products_to_delete:
                        JournalZohoProduct.objects.filter(
                            id__in=products_to_delete,
                            zohoBill=updated_bill
                        ).delete()
                        logger.info(f"Deleted {len(products_to_delete)} journal products not in update")

                # Validate debit/credit balance before verification
                all_products = updated_bill.products.all()
                total_debit = 0
                total_credit = 0

                # Add vendor amount to balance calculation
                if updated_bill.vendor_amount:
                    try:
                        vendor_amount = float(updated_bill.vendor_amount)
                        if updated_bill.vendor_debit_or_credit == 'debit':
                            total_debit += vendor_amount
                        elif updated_bill.vendor_debit_or_credit == 'credit':
                            total_credit += vendor_amount
                    except (ValueError, TypeError):
                        logger.warning(f"Invalid vendor amount: {updated_bill.vendor_amount}")

                # Add IGST amount to balance calculation
                if updated_bill.igst:
                    try:
                        igst_amount = float(updated_bill.igst)
                        if igst_amount > 0:
                            if updated_bill.igst_debit_or_credit == 'debit':
                                total_debit += igst_amount
                            elif updated_bill.igst_debit_or_credit == 'credit':
                                total_credit += igst_amount
                    except (ValueError, TypeError):
                        logger.warning(f"Invalid IGST amount: {updated_bill.igst}")

                # Add CGST amount to balance calculation
                if updated_bill.cgst:
                    try:
                        cgst_amount = float(updated_bill.cgst)
                        if cgst_amount > 0:
                            if updated_bill.cgst_debit_or_credit == 'debit':
                                total_debit += cgst_amount
                            elif updated_bill.cgst_debit_or_credit == 'credit':
                                total_credit += cgst_amount
                    except (ValueError, TypeError):
                        logger.warning(f"Invalid CGST amount: {updated_bill.cgst}")

                # Add SGST amount to balance calculation
                if updated_bill.sgst:
                    try:
                        sgst_amount = float(updated_bill.sgst)
                        if sgst_amount > 0:
                            if updated_bill.sgst_debit_or_credit == 'debit':
                                total_debit += sgst_amount
                            elif updated_bill.sgst_debit_or_credit == 'credit':
                                total_credit += sgst_amount
                    except (ValueError, TypeError):
                        logger.warning(f"Invalid SGST amount: {updated_bill.sgst}")

                # Add product amounts to balance calculation
                for product in all_products:
                    try:
                        amount = float(product.amount) if product.amount else 0
                        if product.debit_or_credit == 'debit':
                            total_debit += amount
                        elif product.debit_or_credit == 'credit':
                            total_credit += amount
                    except (ValueError, TypeError):
                        logger.warning(f"Invalid amount for product {product.id}: {product.amount}")
                        continue

                # Check if debits equal credits (with small tolerance for floating point)
                tolerance = 0.01
                if abs(total_debit - total_credit) > tolerance:
                    return Response({
                        "detail": f"Debit and credit amounts must be equal. Current totals: Debit: {total_debit:.2f}, Credit: {total_credit:.2f}",
                        "debit_total": total_debit,
                        "credit_total": total_credit,
                        "difference": abs(total_debit - total_credit)
                    }, status=status.HTTP_400_BAD_REQUEST)

                # Update bill status only if debit/credit validation passes
                logger.info(f"[DEBUG] journal_bill_verify_view - Updating bill status from '{bill.status}' to 'Verified'")
                bill.status = 'Verified'
                bill.save()
                logger.info(f"[DEBUG] journal_bill_verify_view - Bill status updated successfully to '{bill.status}'")

                # Final verification of vendor data in response
                response_data = JournalZohoBillSerializer(
                    updated_bill, 
                    context={'organization': organization}
                ).data
                logger.info(f"[DEBUG] journal_bill_verify_view - Response vendor data: {response_data.get('vendor')}")
                logger.info(f"[DEBUG] journal_bill_verify_view - Verification process completed successfully")

                return Response(response_data)

            else:
                logger.error(f"[DEBUG] journal_bill_verify_view - Serializer validation FAILED")
                logger.error(f"[DEBUG] journal_bill_verify_view - Serializer errors: {serializer.errors}")

                # Check if vendor-related errors exist and provide helpful message
                if 'vendor' in serializer.errors:
                    logger.error(f"[DEBUG] journal_bill_verify_view - Vendor-specific errors: {serializer.errors['vendor']}")
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

    except JournalBill.DoesNotExist:
        logger.error(f"[DEBUG] journal_bill_verify_view - ERROR: JournalBill not found with ID: {payload_bill_id}, org: {organization.id if organization else 'None'}")
        return Response({"detail": "Journal bill not found"}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        logger.error(f"[DEBUG] journal_bill_verify_view - UNEXPECTED ERROR: {str(e)}")
        logger.error(f"[DEBUG] journal_bill_verify_view - Error type: {type(e).__name__}")
        import traceback
        logger.error(f"[DEBUG] journal_bill_verify_view - Traceback: {traceback.format_exc()}")
        return Response(
            {"detail": f"Verification failed: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@extend_schema(
    responses={"200": {"detail": "journal synced to Zoho successfully"}},
    tags=["Zoho Journal Bills"],
    methods=["POST"]
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def journal_bill_sync_view(request, org_id, bill_id):
    """Sync verified journal bill to Zoho Books. Changes status to 'Synced'."""
    logger.info(f"[DEBUG] journal_bill_sync_view - Starting sync for org_id: {org_id}, bill_id: {bill_id}")

    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        logger.error(f"[DEBUG] journal_bill_sync_view - Organization not found: {org_id}")
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        bill = JournalBill.objects.get(id=bill_id, organization=organization)
        logger.info(f"[DEBUG] journal_bill_sync_view - Found JournalBill: {bill.id}, status: {bill.status}")

        if bill.status != 'Verified':
            logger.error(f"[DEBUG] journal_bill_sync_view - Invalid status for sync: {bill.status}")
            return Response(
                {"detail": "Bill must be in 'Verified' status to sync"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Get Zoho journal data
        try:
            zoho_bill = JournalZohoBill.objects.get(selectBill=bill, organization=organization)
            logger.info(f"[DEBUG] journal_bill_sync_view - Found JournalZohoBill: {zoho_bill.id}")
        except JournalZohoBill.DoesNotExist:
            logger.error(f"[DEBUG] journal_bill_sync_view - JournalZohoBill not found for bill: {bill_id}")
            return Response(
                {"detail": "Zoho journal data not found. Please verify the bill first."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Get Zoho credentials
        try:
            current_token = get_zoho_credentials(organization)
        except ValueError as e:
            return Response(
                {"detail": str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Get products
        zoho_products = zoho_bill.products.all()
        if not zoho_products.exists():
            return Response(
                {"detail": "No products found for this bill"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Prepare data for Zoho API (Journal Entry)
        bill_date_str = zoho_bill.bill_date.strftime('%Y-%m-%d') if zoho_bill.bill_date else None

        bill_data = {
            "reference_number": zoho_bill.bill_no,
            "journal_date": bill_date_str,
            "notes": zoho_bill.note,
            "line_items": []
        }

        # Add mandatory vendor line item
        if zoho_bill.vendor_coa and zoho_bill.vendor_amount:
            vendor_line_item = {
                "description": f"Vendor - {zoho_bill.vendor.companyName if zoho_bill.vendor else 'Unknown Vendor'}",
                "account_id": str(zoho_bill.vendor_coa.accountId),
                "amount": float(zoho_bill.vendor_amount),
                "debit_or_credit": zoho_bill.vendor_debit_or_credit or "credit"
            }
            bill_data["line_items"].append(vendor_line_item)
            logger.info(f"[DEBUG] Added vendor line item: {vendor_line_item}")
        else:
            logger.error(f"[DEBUG] Vendor line item is mandatory but missing vendor_coa or vendor_amount")
            return Response(
                {"detail": "Vendor chart of account and amount are mandatory for journal entry"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Add IGST line item if available
        if zoho_bill.igst and float(zoho_bill.igst or 0) > 0 and zoho_bill.igst_coa:
            igst_line_item = {
                "description": "IGST",
                "account_id": str(zoho_bill.igst_coa.accountId),
                "amount": float(zoho_bill.igst),
                "debit_or_credit": zoho_bill.igst_debit_or_credit or "debit"
            }
            bill_data["line_items"].append(igst_line_item)
            logger.info(f"[DEBUG] Added IGST line item: {igst_line_item}")

        # Add CGST line item if available
        if zoho_bill.cgst and float(zoho_bill.cgst or 0) > 0 and zoho_bill.cgst_coa:
            cgst_line_item = {
                "description": "CGST",
                "account_id": str(zoho_bill.cgst_coa.accountId),
                "amount": float(zoho_bill.cgst),
                "debit_or_credit": zoho_bill.cgst_debit_or_credit or "debit"
            }
            bill_data["line_items"].append(cgst_line_item)
            logger.info(f"[DEBUG] Added CGST line item: {cgst_line_item}")

        # Add SGST line item if available
        if zoho_bill.sgst and float(zoho_bill.sgst or 0) > 0 and zoho_bill.sgst_coa:
            sgst_line_item = {
                "description": "SGST",
                "account_id": str(zoho_bill.sgst_coa.accountId),
                "amount": float(zoho_bill.sgst),
                "debit_or_credit": zoho_bill.sgst_debit_or_credit or "debit"
            }
            bill_data["line_items"].append(sgst_line_item)
            logger.info(f"[DEBUG] Added SGST line item: {sgst_line_item}")

        # Add line items from products
        for item in zoho_products:
            try:
                # Get chart of account for the line item
                chart_of_account = item.chart_of_accounts

                if not chart_of_account:
                    logger.warning(f"No chart of account found for product {item.id}")
                    continue

                line_item = {
                    "description": item.item_details or f"Product Item {item.id}",
                    "account_id": str(chart_of_account.accountId),
                    "amount": float(item.amount) if item.amount else 0,
                    "debit_or_credit": getattr(item, 'debit_or_credit', 'debit')
                }
                bill_data["line_items"].append(line_item)
                logger.info(f"[DEBUG] Added product line item: {line_item}")

            except Exception as e:
                logger.error(f"Error processing product {item.id}: {str(e)}")
                continue

        if not bill_data["line_items"]:
            logger.error(f"[DEBUG] journal_bill_sync_view - No valid line items found for syncing")
            return Response(
                {"detail": "No valid line items found for syncing"},
                status=status.HTTP_400_BAD_REQUEST
            )

        logger.info(f"[DEBUG] journal_bill_sync_view - Prepared {len(bill_data['line_items'])} line items for sync")

        # Sync to Zoho Books
        url = f"https://www.zohoapis.in/books/v3/journals?organization_id={current_token.organisationId}"
        payload = json.dumps(bill_data)
        headers = {
            'Authorization': f'Zoho-oauthtoken {current_token.accessToken}',
            'Content-Type': 'application/json'
        }

        logger.info(f"[DEBUG] journal_bill_sync_view - Making API call to Zoho: {url}")

        try:
            response = requests.post(url, headers=headers, data=payload)

            # Handle token refresh if needed
            if response.status_code == 401:
                new_access_token = refresh_zoho_access_token(current_token)
                if new_access_token:
                    headers['Authorization'] = f'Zoho-oauthtoken {new_access_token}'
                    response = requests.post(url, headers=headers, data=payload)

            if response.status_code == 201:
                logger.info(f"[DEBUG] journal_bill_sync_view - Zoho sync successful")
                # Update bill status
                bill.status = 'Synced'
                bill.save()
                logger.info(f"[DEBUG] journal_bill_sync_view - Bill status updated to Synced")

                response_data = response.json()
                zoho_journal_id = response_data.get('journal', {}).get('journal_id')
                logger.info(f"[DEBUG] journal_bill_sync_view - Zoho journal ID: {zoho_journal_id}")
                return Response({
                    "detail": "journal synced to Zoho successfully",
                    "zoho_journal_id": zoho_journal_id
                })
            else:
                response_json = response.json() if response.content else {}
                error_message = response_json.get("message", "Failed to send data to Zoho")
                logger.error(f"[DEBUG] journal_bill_sync_view - Zoho sync failed: {response.status_code} - {error_message}")
                logger.error(f"[DEBUG] journal_bill_sync_view - Response content: {response.text}")
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

    except JournalBill.DoesNotExist:
        logger.error(f"[DEBUG] journal_bill_sync_view - JournalBill not found: {bill_id}")
        return Response({"detail": "journal bill not found"}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        logger.error(f"[DEBUG] journal_bill_sync_view - Unexpected error: {str(e)}")
        import traceback
        logger.error(f"[DEBUG] journal_bill_sync_view - Traceback: {traceback.format_exc()}")
        return Response(
            {"detail": f"Sync failed: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@extend_schema(
    responses={"200": {"detail": "journal bill deleted successfully"}},
    tags=["Zoho Journal Bills"],
    methods=["DELETE"]
)
@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def journal_bill_delete_view(request, org_id, bill_id):
    """Delete an journal bill and its associated file."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        bill = JournalBill.objects.get(id=bill_id, organization=organization)

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
            "detail": "journal bill and associated file deleted successfully"
        })

    except JournalBill.DoesNotExist:
        return Response({"detail": "journal bill not found"}, status=status.HTTP_404_NOT_FOUND)







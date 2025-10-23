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
    ExpenseBill,
    ExpenseZohoBill,
    ExpenseZohoProduct,
)
from .serializers.common import (
    AnalysisResponseSerializer,
)
from .serializers.expense_bills import (
    ZohoExpenseBillSerializer,
    ZohoExpenseBillDetailSerializer,
    ExpenseZohoBillSerializer,
    ZohoExpenseBillMultipleUploadSerializer,
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
        5. Expense items with descriptions, categories, and amounts
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
            temperature=0.1  # Lower temperature for more consistent results
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


def create_expense_zoho_objects_from_analysis(bill, analyzed_data, organization):
    """
    Create ExpenseZohoBill and ExpenseZohoProduct objects from analyzed data.
    """
    logger.info(f"Creating Expense Zoho objects for bill {bill.id} with analyzed data: {analyzed_data}")

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
    company_name = relevant_data.get('to', {}).get('name', '').strip().lower()
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

    # Create or update ExpenseZohoBill
    try:
        zoho_bill, created = ExpenseZohoBill.objects.get_or_create(
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
            logger.info(f"Created new ExpenseZohoBill: {zoho_bill.id}")
        else:
            logger.info(f"Found existing ExpenseZohoBill: {zoho_bill.id}")
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
            logger.info(f"Updated existing ExpenseZohoBill: {zoho_bill.id}")

        # Delete existing products and recreate them
        existing_products_count = zoho_bill.products.count()
        if existing_products_count > 0:
            zoho_bill.products.all().delete()
            logger.info(f"Deleted {existing_products_count} existing products")

        # Create ExpenseZohoProduct objects for each item
        items = relevant_data.get('items', [])
        logger.info(f"Creating {len(items)} product line items")

        created_products = []
        for idx, item in enumerate(items):
            try:
                amount = item.get('price', 0) * item.get('quantity', 1)
                product = ExpenseZohoProduct.objects.create(
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
                # Let the model generate billmunshiName automatically
                bill = ExpenseBill.objects.create(
                    file=ContentFile(
                        image_io.read(),
                        name=f"BM-Expense-Page-{page_num + 1}-{unique_id}.jpg"
                    ),
                    fileType=file_type,
                    status='Draft',
                    organization=organization,
                    uploaded_by=uploaded_by
                )
                created_bills.append(bill)

    except Exception as e:
        logger.error(f"Error splitting Expense PDF: {str(e)}")
        raise Exception(f"Expense PDF processing failed: {str(e)}")

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
# Expense Bills API Views
# ============================================================================
# ✅
@extend_schema(
    responses=ZohoExpenseBillSerializer(many=True),
    tags=["Zoho Expense Bills"],
    methods=["GET"]
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def expense_bills_list_view(request, org_id):
    """List all expense bills for the organization with pagination."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    bills = ExpenseBill.objects.filter(organization=organization)

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
        serializer = ZohoExpenseBillSerializer(paginated_bills, many=True)
        return paginator.get_paginated_response(serializer.data)

    # Fallback if pagination fails
    serializer = ZohoExpenseBillSerializer(bills, many=True)
    return Response({"results": serializer.data})


# ✅
@extend_schema(
    summary="Upload journal Bills",
    description="Upload single or multiple journal bill files (PDF, JPG, PNG). Supports both single file and multiple file uploads with PDF splitting for multiple invoices.",
    request=ZohoExpenseBillMultipleUploadSerializer,
    responses={201: ZohoExpenseBillSerializer(many=True)},
    tags=["Zoho Expense Bills"],
    methods=["POST"]
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser])
def expense_bill_upload_view(request, org_id):
    """Handle single or multiple expense bill file uploads with PDF splitting support"""

    # Handle both single file and multiple files seamlessly
    files_data = []

    # Debug logging with prints (will show in gunicorn logs)
    print(f"[Expense DEBUG] Request data keys: {list(request.data.keys())}")
    print(f"[Expense DEBUG] 'files' in request.data: {'files' in request.data}")
    print(f"[Expense DEBUG] 'file' in request.data: {'file' in request.data}")

    # Check if files are provided as a list (multiple files)
    if 'files' in request.data:
        files_data = request.data.getlist('files') if hasattr(request.data, 'getlist') else request.data.get('files',
                                                                                                             [])
        # Ensure files_data is always a list
        if not isinstance(files_data, list):
            files_data = [files_data] if files_data else []
        print(f"[Expense DEBUG] Found 'files' field with {len(files_data)} file(s)")
    # Check if a single file is provided
    elif 'file' in request.data:
        single_file = request.data.get('file')
        if single_file:
            files_data = [single_file]
        print(f"[Expense DEBUG] Found 'file' field with {len(files_data)} file(s)")

    print(f"[Expense DEBUG] Total files collected: {len(files_data)}")

    # Debug: Print details about each file
    for i, f in enumerate(files_data):
        print(f"[journal DEBUG] File {i + 1}: {getattr(f, 'name', 'Unknown')} - Size: {getattr(f, 'size', 'Unknown')}")

    # Prepare data for serializer validation
    serializer_data = {
        'files': files_data,
        'fileType': request.data.get('fileType', 'Single Invoice/File')
    }

    print(
        f"[Expense DEBUG] Serializer data: files count = {len(serializer_data['files'])}, fileType = {serializer_data['fileType']}")

    serializer = ZohoExpenseBillMultipleUploadSerializer(data=serializer_data)
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

    print(f"[Expense DEBUG] After serializer validation - files count: {len(files)}, fileType: {file_type}")

    if not files:
        return Response(
            {'error': 'No files provided for upload'},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        # Temporarily removing atomic transaction to debug
        # with transaction.atomic():
        print(f"[Expense DEBUG] Starting to process {len(files)} files")
        for i, uploaded_file in enumerate(files):
            print(f"[Expense DEBUG] Processing file {i + 1}/{len(files)}: {uploaded_file.name}")
            file_extension = uploaded_file.name.lower().split('.')[-1]

            # Handle PDF splitting for multiple invoice files
            if file_type == 'Multiple Invoice/File' and file_extension == 'pdf':
                print(f"[Expense DEBUG] Processing as PDF split for file: {uploaded_file.name}")
                pdf_bills = process_pdf_splitting_expense(
                    uploaded_file, organization, file_type, request.user
                )
                print(f"[Expense DEBUG] PDF splitting created {len(pdf_bills)} bills")
                created_bills.extend(pdf_bills)
            else:
                # Create single bill (including PDFs for single invoice type)
                # Let the model generate billmunshiName automatically
                print(f"[Expense DEBUG] Creating single bill for file: {uploaded_file.name}")
                bill = ExpenseBill.objects.create(
                    file=uploaded_file,
                    fileType=file_type,
                    status='Draft',
                    organization=organization,
                    uploaded_by=request.user
                )
                print(f"[Expense DEBUG] Created bill: {bill.billmunshiName} (ID: {bill.id})")
                created_bills.append(bill)

        print(f"[Expense DEBUG] Completed processing all files. Total bills created: {len(created_bills)}")

        # Debug: Print all created bills
        for i, bill in enumerate(created_bills):
            print(f"[Expense DEBUG] Bill {i + 1}: {bill.billmunshiName} (ID: {bill.id})")

        response_serializer = ZohoExpenseBillSerializer(created_bills, many=True, context={'request': request})

        # Log the successful result
        logger.info(f"Successfully processed {len(files)} files and created {len(created_bills)} bills")
        for i, bill in enumerate(created_bills):
            logger.info(f"Created bill {i + 1}: {bill.billmunshiName} (ID: {bill.id})")

        return Response({
            'message': f'Successfully uploaded {len(files)} file(s) and created {len(created_bills)} bill(s)',
            'files_uploaded': len(files),
            'bills_created': len(created_bills),
            'bills': response_serializer.data
        }, status=status.HTTP_201_CREATED)

    except Exception as e:
        logger.error(f"Error uploading Expense bills: {str(e)}")
        logger.error(f"Exception type: {type(e).__name__}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return Response({
            'detail': f'Error processing files: {str(e)}'
        }, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(
    responses=ZohoExpenseBillDetailSerializer,
    tags=["Zoho Expense Bills"],
    methods=["GET"]
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def expense_bill_detail_view(request, org_id, bill_id):
    """Get expense bill details including analysis data."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        # Fetch the ExpenseBill
        bill = ExpenseBill.objects.get(id=bill_id, organization=organization)

        # Get the next bill with 'Analysed' status
        next_bill_id = None
        analysed_bills = ExpenseBill.objects.filter(
            organization=organization,
            status='Analysed'
        ).exclude(id=bill_id).values_list('id', flat=True)

        if analysed_bills:
            next_bill_id = str(analysed_bills[0])  # Get the first analysed bill
            logger.info(f"Found next analysed Expense bill: {next_bill_id}")
        else:
            logger.info("No analysed Expense bills found for next_bill")

        # Always set next_bill on the bill object
        bill.next_bill = next_bill_id

        # Get the related ExpenseZohoBill if it exists
        try:
            zoho_bill = ExpenseZohoBill.objects.select_related('expense_bill').prefetch_related(
                'products'
            ).get(expense_bill=bill, organization=organization)

            # Attach zoho_bill to the bill object for the serializer
            bill.zoho_bill = zoho_bill
        except ExpenseZohoBill.DoesNotExist:
            # If no ExpenseZohoBill exists, set it to None
            bill.zoho_bill = None

        # Serialize the data with request context for full URLs
        serializer = ZohoExpenseBillDetailSerializer(bill, context={'request': request})
        return Response(serializer.data)

    except ExpenseBill.DoesNotExist:
        return Response({"detail": "Expense bill not found"}, status=status.HTTP_404_NOT_FOUND)


# ✅
@extend_schema(
    responses=AnalysisResponseSerializer,
    tags=["Zoho Expense Bills"],
    methods=["POST"]
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def expense_bill_analyze_view(request, org_id, bill_id):
    """Analyze Expense bill using OpenAI. Changes status from 'Draft' to 'Analyzed'."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        bill = ExpenseBill.objects.get(id=bill_id, organization=organization)

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
        create_expense_zoho_objects_from_analysis(bill, analyzed_data, organization)

        return Response({
            "detail": "Bill analyzed successfully",
            "analyzed_data": analyzed_data
        })

    except ExpenseBill.DoesNotExist:
        return Response({"detail": "Expense bill not found"}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        logger.error(f"Analysis failed: {str(e)}")
        return Response(
            {"detail": f"Analysis failed: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )

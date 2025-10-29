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
            {'error': 'Organization not found'},
            status=status.HTTP_400_BAD_REQUEST
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

        response_serializer = TallyExpenseBillSerializer(created_bills, many=True, context={'request': request})
        return Response({
            'message': f'Successfully uploaded {len(files)} file(s) and created {len(created_bills)} bill(s)',
            'files_uploaded': len(files),
            'bills_created': len(created_bills),
            'bills': response_serializer.data
        }, status=status.HTTP_201_CREATED)

    except Exception as e:
        logger.error(f"Error uploading expense bills: {str(e)}")
        return Response(
            {'error': f'Error processing files: {str(e)}'},
            status=status.HTTP_400_BAD_REQUEST
        )


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
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

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
        return Response(
            {'data': 'Bill is already Processed'},
            status=status.HTTP_200_OK
        )

    try:
        # Check if bill already has analyzed data
        if bill.analysed_data:
            logger.info(f"Using existing analyzed data for expense bill {bill_id}")
            analyzed_bill = process_existing_expense_analysis_data(bill, bill.analysed_data, organization)
        else:
            logger.info(f"Running new OpenAI analysis for expense bill {bill_id}")
            analyzed_bill = analyze_expense_bill_with_ai(bill, organization)

        serializer = TallyExpenseAnalyzedBillSerializer(analyzed_bill)
        return Response(serializer.data, status=status.HTTP_200_OK)

    except Exception as e:
        logger.error(f"Expense bill analysis failed: {str(e)}")
        return Response(
            {'error': f'Analysis failed: {str(e)}'},
            status=status.HTTP_400_BAD_REQUEST
        )


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
    responses={200: TallyExpenseBillSerializer},
    tags=['Tally Expense Bills']
)
@api_view(['GET'])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def expense_bill_detail(request, org_id, bill_id):
    """Get expense bill detail including analysis data"""
    organization = get_organization_from_request(request, org_id)
    if not organization:
        return Response(
            {'error': 'Organization not found'},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        # Fetch the TallyExpenseBill
        bill = TallyExpenseBill.objects.get(
            id=bill_id,
            organization=organization
        )
        # Get a random bill with 'Analysed' status
        next_bill_id = None
        analysed_bills = TallyExpenseBill.objects.filter(
            organization=organization,
            status=TallyExpenseBill.BillStatus.ANALYSED
        ).exclude(id=bill_id).values_list('id', flat=True)

        if analysed_bills:
            next_bill_id = str(random.choice(list(analysed_bills)))

        # Get the related TallyExpenseAnalyzedBill if it exists
        try:
            analyzed_bill = TallyExpenseAnalyzedBill.objects.select_related(
                'vendor', 'igst_taxes', 'cgst_taxes', 'sgst_taxes'
            ).prefetch_related(
                'products__chart_of_accounts'
            ).get(selected_bill=bill, organization=organization)

            # Get vendor ledger
            vendor_ledger = analyzed_bill.vendor

            # Get analyzed bill products
            analyzed_bill_products = analyzed_bill.products.all()

            # Format bill date
            bill_date_str = analyzed_bill.bill_date.strftime('%d-%m-%Y') if analyzed_bill.bill_date else None

            # Get organization name as team_slug
            team_slug = organization.name if hasattr(organization, 'name') else str(organization.id)

            # Structure the analyzed data in expense format
            bill_data = {
                "name": vendor_ledger.name if vendor_ledger else "No Ledger",
                "voucher": analyzed_bill.voucher or "",
                "bill_no": analyzed_bill.bill_no,
                "bill_date": bill_date_str,
                "total": float(analyzed_bill.total or 0),
                "vendor_debit_or_credit": analyzed_bill.vendor_debit_or_credit,
                "vendor_amount": float(analyzed_bill.vendor_amount or 0),
                "company_id": team_slug,
                "taxes": {
                    "igst": {
                        "amount": float(analyzed_bill.igst or 0),
                        "ledger": str(analyzed_bill.igst_taxes) if analyzed_bill.igst_taxes else "No Tax Ledger",
                        "debit_or_credit": analyzed_bill.igst_debit_or_credit,
                    },
                    "cgst": {
                        "amount": float(analyzed_bill.cgst or 0),
                        "ledger": str(analyzed_bill.cgst_taxes) if analyzed_bill.cgst_taxes else "No Tax Ledger",
                        "debit_or_credit": analyzed_bill.cgst_debit_or_credit,
                    },
                    "sgst": {
                        "amount": float(analyzed_bill.sgst or 0),
                        "ledger": str(analyzed_bill.sgst_taxes) if analyzed_bill.sgst_taxes else "No Tax Ledger",
                        "debit_or_credit": analyzed_bill.sgst_debit_or_credit,
                    }
                },
                "expense_items": [
                    {
                        "item_id": str(item.id),
                        "item_details": item.item_details,
                        "chart_of_accounts": str(item.chart_of_accounts) if item.chart_of_accounts else "No COA Ledger",
                        "amount": float(item.amount or 0),
                        "debit_or_credit": item.debit_or_credit,
                    }
                    for item in analyzed_bill_products
                ],
            }

            # Include the base bill information
            bill_serializer = TallyExpenseBillSerializer(bill, context={'request': request})

            response_data = {
                "bill": bill_serializer.data,
                "analyzed_data": bill_data,
                "analyzed_bill": analyzed_bill.id,
                "next_bill": next_bill_id
            }

            return Response(response_data)

        except TallyExpenseAnalyzedBill.DoesNotExist:
            # If no analyzed bill exists, return just the base bill info
            bill_serializer = TallyExpenseBillSerializer(bill, context={'request': request})
            return Response({
                "bill": bill_serializer.data,
                "analyzed_data": None,
                "message": "Bill has not been analyzed yet"
            })

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
            {'error': 'Organization not found'},
            status=status.HTTP_400_BAD_REQUEST
        )

    if not bill_id or not analyzed_bill_id:
        return Response(
            {'error': 'bill_id and analyzed_bill are required'},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        bill = TallyExpenseBill.objects.get(id=bill_id, organization=organization)
        analyzed_bill = TallyExpenseAnalyzedBill.objects.get(id=analyzed_bill_id, organization=organization)
    except (TallyExpenseBill.DoesNotExist, TallyExpenseAnalyzedBill.DoesNotExist):
        return Response(
            {'error': 'Bill or analyzed data not found'},
            status=status.HTTP_404_NOT_FOUND
        )

    if bill.status not in [TallyVendorBill.BillStatus.ANALYSED, TallyVendorBill.BillStatus.VERIFIED]:
        return Response(
            {"detail": "Bill must be in 'Analysed' or 'Verified' status to save"},
            status=status.HTTP_400_BAD_REQUEST
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
        return Response(
            {'error': f'Verification failed: {str(e)}'},
            status=status.HTTP_400_BAD_REQUEST
        )


def update_analyzed_expense_bill_data(analyzed_bill, analyzed_data, organization):
    """Update analyzed expense bill with user modifications"""

    if not analyzed_data:
        return analyzed_bill

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

    # Optionally delete products that weren't included in the update
    # (commented out to preserve existing behavior)
    # products_to_delete = set(existing_products.keys()) - updated_product_ids
    # if products_to_delete:
    #     TallyExpenseAnalyzedProduct.objects.filter(
    #         id__in=products_to_delete,
    #         expense_bill=analyzed_bill
    #     ).delete()


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
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    bill_id = serializer.validated_data['bill_id']
    organization = get_organization_from_request(request, org_id)

    try:
        bill = TallyExpenseBill.objects.get(id=bill_id, organization=organization)
        analyzed_bill = TallyExpenseAnalyzedBill.objects.get(selected_bill=bill)
    except (TallyExpenseBill.DoesNotExist, TallyExpenseAnalyzedBill.DoesNotExist):
        return Response(
            {'error': 'Bill or analyzed data not found'},
            status=status.HTTP_404_NOT_FOUND
        )

    if bill.status != TallyExpenseBill.BillStatus.VERIFIED:
        return Response(
            {'error': 'Bill is not verified'},
            status=status.HTTP_400_BAD_REQUEST
        )

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
        return Response(
            {'error': f'Sync failed: {str(e)}'},
            status=status.HTTP_400_BAD_REQUEST
        )


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
            {'error': 'Organization not found'},
            status=status.HTTP_400_BAD_REQUEST
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
            {'error': 'Organization not found'},
            status=status.HTTP_400_BAD_REQUEST
        )

    status_filter = request.query_params.get('status')

    if not status_filter:
        return Response(
            {'error': 'Status parameter is required'},
            status=status.HTTP_400_BAD_REQUEST
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
            {'error': 'Organization not found'},
            status=status.HTTP_400_BAD_REQUEST
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
    """Prepare expense bill data for Tally sync using structured format"""
    vendor_ledger = analyzed_bill.vendor
    analyzed_bill_products = analyzed_bill.products.all()
    bill_date_str = analyzed_bill.bill_date.strftime('%d-%m-%Y') if analyzed_bill.bill_date else None

    # Initialize DR and CR ledgers for expense sync
    dr_ledger = []
    cr_ledger = []

    # Process expense line items based on their debit_or_credit field
    total_debit = 0
    total_credit = 0

    for item in analyzed_bill_products:
        if item.amount and item.amount > 0:
            ledger_entry = {
                "LEDGERNAME": str(item.chart_of_accounts) if item.chart_of_accounts else "No COA Ledger",
                "AMOUNT": float(item.amount)
            }

            # Simple rule: debit goes to DR_LEDGER, credit goes to CR_LEDGER
            if item.debit_or_credit == 'debit':
                dr_ledger.append(ledger_entry)
                total_debit += float(item.amount)
            elif item.debit_or_credit == 'credit':
                cr_ledger.append(ledger_entry)
                total_credit += float(item.amount)

    # Process IGST based on debit_or_credit field
    if analyzed_bill.igst and analyzed_bill.igst > 0 and analyzed_bill.igst_taxes:
        igst_entry = {
            "LEDGERNAME": str(analyzed_bill.igst_taxes),
            "AMOUNT": float(analyzed_bill.igst)
        }
        if analyzed_bill.igst_debit_or_credit == 'debit':
            dr_ledger.append(igst_entry)
            total_debit += float(analyzed_bill.igst)
        elif analyzed_bill.igst_debit_or_credit == 'credit':
            cr_ledger.append(igst_entry)
            total_credit += float(analyzed_bill.igst)

    # Process CGST based on debit_or_credit field
    if analyzed_bill.cgst and analyzed_bill.cgst > 0 and analyzed_bill.cgst_taxes:
        cgst_entry = {
            "LEDGERNAME": str(analyzed_bill.cgst_taxes),
            "AMOUNT": float(analyzed_bill.cgst)
        }
        if analyzed_bill.cgst_debit_or_credit == 'debit':
            dr_ledger.append(cgst_entry)
            total_debit += float(analyzed_bill.cgst)
        elif analyzed_bill.cgst_debit_or_credit == 'credit':
            cr_ledger.append(cgst_entry)
            total_credit += float(analyzed_bill.cgst)

    # Process SGST based on debit_or_credit field
    if analyzed_bill.sgst and analyzed_bill.sgst > 0 and analyzed_bill.sgst_taxes:
        sgst_entry = {
            "LEDGERNAME": str(analyzed_bill.sgst_taxes),
            "AMOUNT": float(analyzed_bill.sgst)
        }
        if analyzed_bill.sgst_debit_or_credit == 'debit':
            dr_ledger.append(sgst_entry)
            total_debit += float(analyzed_bill.sgst)
        elif analyzed_bill.sgst_debit_or_credit == 'credit':
            cr_ledger.append(sgst_entry)
            total_credit += float(analyzed_bill.sgst)

    # Process vendor based on vendor_debit_or_credit field using vendor_amount
    if vendor_ledger and analyzed_bill.vendor_amount and analyzed_bill.vendor_amount > 0:
        vendor_entry = {
            "LEDGERNAME": vendor_ledger.name,
            "AMOUNT": float(analyzed_bill.vendor_amount)
        }

        # Add vendor to appropriate ledger based on vendor_debit_or_credit
        if analyzed_bill.vendor_debit_or_credit == 'debit':
            dr_ledger.append(vendor_entry)
            total_debit += float(analyzed_bill.vendor_amount)
        elif analyzed_bill.vendor_debit_or_credit == 'credit':
            cr_ledger.append(vendor_entry)
            total_credit += float(analyzed_bill.vendor_amount)

    # Ensure debit and credit are balanced - remove automatic vendor balancing
    # since vendor is now explicitly handled based on vendor_debit_or_credit
    # The previous logic is commented out:
    # if total_debit > 0 and total_credit == 0:
    #     cr_ledger.append({
    #         "LEDGERNAME": vendor_ledger.name if vendor_ledger else "No Vendor Ledger",
    #         "AMOUNT": total_debit
    #     })
    # elif total_credit > 0 and total_debit == 0:
    #     dr_ledger.append({
    #         "LEDGERNAME": vendor_ledger.name if vendor_ledger else "No Vendor Ledger",
    #         "AMOUNT": total_credit
    #     })

    # Build expense sync payload with structured format similar to vendor bills
    bill_data = {
        "id": analyzed_bill.id,
        "voucher": analyzed_bill.voucher or "",
        "bill_no": analyzed_bill.bill_no,
        "bill_date": bill_date_str,
        "total": float(analyzed_bill.total or 0),
        "name": vendor_ledger.name if vendor_ledger and vendor_ledger.name else "No Ledger",
        "company": vendor_ledger.company if vendor_ledger and vendor_ledger.company else "No Ledger",
        "gst_in": vendor_ledger.gst_in if vendor_ledger and vendor_ledger.gst_in else "No Ledger",
        "DR_LEDGER": dr_ledger,
        "CR_LEDGER": cr_ledger,
        "notes": analyzed_bill.note or "AI Analyzed Expense Bill",
        "created_at": analyzed_bill.created_at
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
        return Response(
            {'error': f'External expense sync failed: {str(e)}'},
            status=status.HTTP_400_BAD_REQUEST
        )

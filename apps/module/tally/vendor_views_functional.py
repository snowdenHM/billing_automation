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
from django.db.models import Q
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, OpenApiResponse
from pdf2image import convert_from_bytes
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, parser_classes
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.pagination import DefaultPagination
from apps.organizations.models import Organization
from apps.common.permissions import IsOrgAdmin
from .models import (
    TallyVendorBill,
    TallyVendorAnalyzedBill,
    TallyVendorAnalyzedProduct,
    Ledger,
    ParentLedger
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
    """Analyze bill using OpenAI API"""
    if not client:
        raise Exception("OpenAI client not configured")

    # Read file and convert to base64
    try:
        with open(bill.file.path, 'rb') as f:
            image_base64 = base64.b64encode(f.read()).decode('utf-8')
    except Exception as e:
        raise Exception(f"Error reading bill file: {str(e)}")

    # Invoice schema for AI extraction
    invoice_schema = {
        "$schema": "http://json-schema.org/draft/2020-12/schema",
        "title": "Invoice",
        "description": "A simple invoice format",
        "type": "object",
        "properties": {
            "invoiceNumber": {"type": "string"},
            "dateIssued": {"type": "string", "format": "date"},
            "dueDate": {"type": "string", "format": "date"},
            "from": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "address": {"type": "string"}
                }
            },
            "to": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "address": {"type": "string"}
                }
            },
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "quantity": {"type": "number"},
                        "price": {"type": "number"}
                    }
                }
            },
            "total": {"type": "number"},
            "igst": {"type": "number"},
            "cgst": {"type": "number"},
            "sgst": {"type": "number"}
        }
    }

    # AI processing request
    try:
        response = client.chat.completions.create(
            model='gpt-4o',
            response_format={"type": "json_object"},
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"Extract invoice data in JSON format using this schema: {json.dumps(invoice_schema)}"
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}
                    }
                ]
            }],
            max_tokens=1000
        )
        json_data = json.loads(response.choices[0].message.content)
    except Exception as e:
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
    """Find matching vendor ledger"""
    try:
        parent_ledger = ParentLedger.objects.get(
            parent="Sundry Creditors",
            organization=organization
        )
        vendor_list = Ledger.objects.filter(
            parent=parent_ledger,
            organization=organization
        )

        # Find matching vendor (case-insensitive exact match first)
        vendor = vendor_list.filter(name__iexact=company_name).first()
        if not vendor:
            vendor = vendor_list.filter(name__icontains=company_name).first()

        return vendor

    except ParentLedger.DoesNotExist:
        return None


def process_pdf_splitting(pdf_file, organization, file_type):
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

                # Create bill for this page
                bill = TallyVendorBill.objects.create(
                    file=ContentFile(
                        image_io.read(),
                        name=f"BM-Page-{page_num + 1}-{unique_id}.jpg"
                    ),
                    file_type=file_type,
                    organization=organization
                )
                created_bills.append(bill)

    except Exception as e:
        logger.error(f"Error splitting PDF: {str(e)}")
        raise Exception(f"PDF processing failed: {str(e)}")

    return created_bills


def prepare_sync_data(analyzed_bill, organization):
    """Prepare bill data for Tally sync"""
    vendor_ledger = analyzed_bill.vendor

    # Get products for this analyzed bill
    products = analyzed_bill.products.all()
    products_data = []

    for product in products:
        product_data = {
            "id": str(product.id),
            "item_name": product.item_name or "",
            "item_details": product.item_details or "",
            "taxes": str(product.taxes.id) if product.taxes else None,
            "price": str(product.price or 0),
            "quantity": product.quantity or 0,
            "amount": str(product.amount or 0),
            "product_gst": product.product_gst or "",
            "igst": str(product.igst or 0),
            "cgst": str(product.cgst or 0),
            "sgst": str(product.sgst or 0),
            "created_at": product.created_at.isoformat() if hasattr(product, 'created_at') else None
        }
        products_data.append(product_data)

    # Build sync payload
    bill_data = {
        "id": str(analyzed_bill.id),
        "selected_bill": str(analyzed_bill.selected_bill.id),
        "selected_bill_name": analyzed_bill.selected_bill.file.name.split('/')[-1] if analyzed_bill.selected_bill.file else "",
        "vendor": str(vendor_ledger.id) if vendor_ledger else None,
        "vendor_name": vendor_ledger.name if vendor_ledger else "",
        "bill_no": analyzed_bill.bill_no or "",
        "bill_date": analyzed_bill.bill_date.strftime('%d-%m-%Y') if analyzed_bill.bill_date else None,
        "total": str(analyzed_bill.total or 0),
        "igst": str(analyzed_bill.igst or 0),
        "igst_taxes": str(analyzed_bill.igst_taxes.id) if analyzed_bill.igst_taxes else None,
        "cgst": str(analyzed_bill.cgst or 0),
        "cgst_taxes": str(analyzed_bill.cgst_taxes.id) if analyzed_bill.cgst_taxes else None,
        "sgst": str(analyzed_bill.sgst or 0),
        "sgst_taxes": str(analyzed_bill.sgst_taxes.id) if analyzed_bill.sgst_taxes else None,
        "gst_type": analyzed_bill.gst_type,
        "note": analyzed_bill.note or "",
        "products": products_data,
        "created_at": analyzed_bill.created_at.isoformat() if hasattr(analyzed_bill, 'created_at') else None
    }

    return {"data": bill_data}


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

    bills = TallyVendorBill.objects.filter(
        organization=organization
    ).order_by('-created_at')

    # Pagination
    paginator = DefaultPagination()
    page = paginator.paginate_queryset(bills, request)
    if page is not None:
        serializer = TallyVendorBillSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    serializer = TallyVendorBillSerializer(bills, many=True)
    return Response(serializer.data)

#✅
@extend_schema(
    summary="Upload Vendor Bills",
    description="Upload single or multiple vendor bill files (PDF, JPG, PNG)",
    request=VendorBillUploadSerializer,
    responses={201: TallyVendorBillSerializer(many=True)},
    tags=['Tally Vendor Bills']
)
@api_view(['POST'])
@permission_classes([IsAuthenticated, IsOrgAdmin])
@parser_classes([MultiPartParser, FormParser])
def vendor_bills_upload(request, org_id):
    """Handle vendor bill file uploads with PDF splitting support"""
    serializer = VendorBillUploadSerializer(data=request.data)
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

    try:
        with transaction.atomic():
            for uploaded_file in files:
                file_extension = uploaded_file.name.lower().split('.')[-1]

                # Handle PDF splitting for multiple invoice files
                if (file_type == TallyVendorBill.BillType.MULTI and
                        file_extension == 'pdf'):

                    pdf_bills = process_pdf_splitting(
                        uploaded_file, organization, file_type
                    )
                    created_bills.extend(pdf_bills)
                else:
                    # Create single bill for non-PDF or single invoice type
                    bill = TallyVendorBill.objects.create(
                        file=uploaded_file,
                        file_type=file_type,
                        organization=organization
                    )
                    created_bills.append(bill)

        response_serializer = TallyVendorBillSerializer(created_bills, many=True)
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)

    except Exception as e:
        logger.error(f"Error uploading vendor bills: {str(e)}")
        return Response(
            {'error': f'Error processing files: {str(e)}'},
            status=status.HTTP_400_BAD_REQUEST
        )

# ✅
@extend_schema(
    summary="Get Draft Bills",
    description="Get all draft vendor bills for the organization",
    responses={200: TallyVendorBillSerializer(many=True)},
    tags=['Tally Vendor Bills']
)
@api_view(['GET'])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def vendor_bills_drafts(request, org_id):
    """Get all draft vendor bills"""
    organization = get_organization_from_request(request, org_id)
    if not organization:
        return Response(
            {'error': 'Organization not found'},
            status=status.HTTP_400_BAD_REQUEST
        )

    draft_bills = TallyVendorBill.objects.filter(
        organization=organization,
        status=TallyVendorBill.BillStatus.DRAFT
    ).order_by('-created_at')

    serializer = TallyVendorBillSerializer(draft_bills, many=True)
    return Response(serializer.data)

# ✅
@extend_schema(
    summary="Get Analyzed Bills",
    description="Get all analyzed vendor bills for the organization",
    responses={200: TallyVendorBillSerializer(many=True)},
    tags=['Tally Vendor Bills']
)
@api_view(['GET'])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def vendor_bills_analyzed(request, org_id):
    """Get all analyzed vendor bills"""
    organization = get_organization_from_request(request, org_id)
    if not organization:
        return Response(
            {'error': 'Organization not found'},
            status=status.HTTP_400_BAD_REQUEST
        )

    analyzed_bills = TallyVendorBill.objects.filter(
        Q(organization=organization) &
        (Q(status=TallyVendorBill.BillStatus.ANALYSED) |
         Q(status=TallyVendorBill.BillStatus.VERIFIED))
    ).order_by('-created_at')

    serializer = TallyVendorBillSerializer(analyzed_bills, many=True)
    return Response(serializer.data)

# ✅
@extend_schema(
    summary="Get Synced Bills",
    description="Get all synced vendor bills for the organization",
    responses={200: TallyVendorBillSerializer(many=True)},
    tags=['Tally Vendor Bills']
)
@api_view(['GET'])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def vendor_bills_synced(request, org_id):
    """Get all synced vendor bills"""
    organization = get_organization_from_request(request, org_id)
    if not organization:
        return Response(
            {'error': 'Organization not found'},
            status=status.HTTP_400_BAD_REQUEST
        )

    synced_bills = TallyVendorBill.objects.filter(
        organization=organization,
        status=TallyVendorBill.BillStatus.SYNCED
    ).order_by('-created_at')

    serializer = TallyVendorBillSerializer(synced_bills, many=True)
    return Response(serializer.data)


@extend_schema(
    summary="Get Bill Detail",
    description="Get detailed information about a specific vendor bill",
    responses={200: TallyVendorBillSerializer},
    tags=['Tally Vendor Bills']
)
@api_view(['GET'])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def vendor_bill_detail(request, org_id, bill_id):
    """Get vendor bill detail"""
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

    serializer = TallyVendorBillSerializer(bill)
    return Response(serializer.data)


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

    if bill.status != TallyVendorBill.BillStatus.DRAFT:
        return Response(
            {'error': 'Bill is not in draft status'},
            status=status.HTTP_400_BAD_REQUEST
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
    """Verify analyzed vendor bill"""
    serializer = BillVerificationSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    bill_id = request.data.get('bill_id')
    organization = get_organization_from_request(request, org_id)

    try:
        bill = TallyVendorBill.objects.get(id=bill_id, organization=organization)
        analyzed_bill = TallyVendorAnalyzedBill.objects.get(selected_bill=bill)
    except (TallyVendorBill.DoesNotExist, TallyVendorAnalyzedBill.DoesNotExist):
        return Response(
            {'error': 'Bill or analyzed data not found'},
            status=status.HTTP_404_NOT_FOUND
        )

    if bill.status != TallyVendorBill.BillStatus.ANALYSED:
        return Response(
            {'error': 'Bill is not in analyzed status'},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        verified_bill = verify_bill_data(analyzed_bill, serializer.validated_data, organization)

        # Update bill status
        bill.status = TallyVendorBill.BillStatus.VERIFIED
        bill.save(update_fields=['status'])

        response_serializer = TallyVendorAnalyzedBillSerializer(verified_bill)
        return Response(response_serializer.data, status=status.HTTP_200_OK)

    except Exception as e:
        logger.error(f"Bill verification failed: {str(e)}")
        return Response(
            {'error': f'Verification failed: {str(e)}'},
            status=status.HTTP_400_BAD_REQUEST
        )


def verify_bill_data(analyzed_bill, verification_data, organization):
    """Verify and update bill data"""
    with transaction.atomic():
        # Update bill fields
        if 'vendor_id' in verification_data and verification_data['vendor_id']:
            try:
                analyzed_bill.vendor = Ledger.objects.get(
                    id=verification_data['vendor_id'],
                    organization=organization
                )
            except Ledger.DoesNotExist:
                pass

        # Update basic fields
        for field in ['bill_no', 'bill_date', 'note', 'igst', 'cgst', 'sgst']:
            if field in verification_data:
                setattr(analyzed_bill, field, verification_data[field])

        # Update tax ledgers
        tax_fields = ['igst_taxes_id', 'cgst_taxes_id', 'sgst_taxes_id']
        for tax_field in tax_fields:
            if tax_field in verification_data and verification_data[tax_field]:
                ledger_field = tax_field.replace('_id', '')
                try:
                    tax_ledger = Ledger.objects.get(
                        id=verification_data[tax_field],
                        organization=organization
                    )
                    setattr(analyzed_bill, ledger_field, tax_ledger)
                except Ledger.DoesNotExist:
                    pass

        analyzed_bill.save()

        # Update products if provided
        if 'products' in verification_data:
            update_products(analyzed_bill, verification_data['products'], organization)

        # Validate tax calculations
        validate_tax_calculations(analyzed_bill)

        return analyzed_bill


def update_products(analyzed_bill, products_data, organization):
    """Update analyzed products"""
    for product_data in products_data:
        try:
            product = TallyVendorAnalyzedProduct.objects.get(
                id=product_data['id'],
                vendor_bill_analyzed=analyzed_bill
            )

            # Update product fields
            for field in ['item_name', 'item_details', 'price', 'quantity', 'amount', 'product_gst']:
                if field in product_data:
                    setattr(product, field, product_data[field])

            # Update taxes ledger
            if 'taxes_id' in product_data and product_data['taxes_id']:
                try:
                    product.taxes = Ledger.objects.get(
                        id=product_data['taxes_id'],
                        organization=organization
                    )
                except Ledger.DoesNotExist:
                    pass

            # Calculate GST amounts based on product_gst and bill's gst_type
            if 'product_gst' in product_data:
                calculate_product_gst(product, analyzed_bill)

            product.save()

        except TallyVendorAnalyzedProduct.DoesNotExist:
            continue


def calculate_product_gst(product, analyzed_bill):
    """Calculate GST amounts for product"""
    try:
        if product.product_gst and product.amount:
            gst_percent = float(product.product_gst.strip('%')) if "%" in product.product_gst else 0
            gst_amount = (gst_percent / 100) * float(product.amount or 0)

            # Reset GST amounts
            product.igst = 0
            product.cgst = 0
            product.sgst = 0

            if analyzed_bill.gst_type == TallyVendorAnalyzedBill.GSTType.IGST:
                product.igst = round(gst_amount, 2)
            elif analyzed_bill.gst_type == TallyVendorAnalyzedBill.GSTType.CGST_SGST:
                product.cgst = round(gst_amount / 2, 2)
                product.sgst = round(gst_amount / 2, 2)

    except (ValueError, TypeError) as e:
        logger.warning(f"GST calculation failed for product {product.id}: {e}")


def validate_tax_calculations(analyzed_bill):
    """Validate that bill taxes match sum of product taxes"""
    products = analyzed_bill.products.all()

    total_product_igst = sum(p.igst or 0 for p in products)
    total_product_cgst = sum(p.cgst or 0 for p in products)
    total_product_sgst = sum(p.sgst or 0 for p in products)

    if analyzed_bill.gst_type == TallyVendorAnalyzedBill.GSTType.IGST:
        if abs(total_product_igst - (analyzed_bill.igst or 0)) > 0.01:
            raise Exception(
                f"IGST mismatch: Product total={total_product_igst}, Bill total={analyzed_bill.igst}"
            )
    elif analyzed_bill.gst_type == TallyVendorAnalyzedBill.GSTType.CGST_SGST:
        if (abs(total_product_cgst - (analyzed_bill.cgst or 0)) > 0.01 or
                abs(total_product_sgst - (analyzed_bill.sgst or 0)) > 0.01):
            raise Exception(
                f"CGST/SGST mismatch: Product CGST/SGST={total_product_cgst}/{total_product_sgst}, "
                f"Bill CGST/SGST={analyzed_bill.cgst}/{analyzed_bill.sgst}"
            )


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
        sync_data = prepare_sync_data(analyzed_bill, organization)

        # Update bill status to synced
        bill.status = TallyVendorBill.BillStatus.SYNCED
        bill.save(update_fields=['status'])

        return Response(sync_data, status=status.HTTP_200_OK)

    except Exception as e:
        logger.error(f"Bill sync failed: {str(e)}")
        return Response(
            {'error': f'Sync failed: {str(e)}'},
            status=status.HTTP_400_BAD_REQUEST
        )


@extend_schema(
    summary="Get All Synced Bills",
    description="Get all synced bills with their products for the organization",
    responses={200: BillSyncResponseSerializer(many=True)},
    tags=['Tally TCP']
)
@api_view(['GET'])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def vendor_bills_sync_list(request, org_id):
    """Get all synced bills with their products"""
    organization = get_organization_from_request(request, org_id)

    if not organization:
        return Response(
            {'error': 'Organization not found'},
            status=status.HTTP_400_BAD_REQUEST
        )

    # Get all analyzed bills where the main bill status is "Synced"
    analyzed_bills = TallyVendorAnalyzedBill.objects.filter(
        organization=organization,
        selected_bill__status=TallyVendorBill.BillStatus.SYNCED
    ).select_related(
        'selected_bill', 'vendor', 'igst_taxes', 'cgst_taxes', 'sgst_taxes'
    ).prefetch_related(
        'products__taxes'
    ).order_by('-created_at')

    # Convert each analyzed bill to the new sync format and extract just the data portion
    bills_data = []
    for analyzed_bill in analyzed_bills:
        sync_data = prepare_sync_data(analyzed_bill, organization)
        # Extract the data portion (remove the wrapper)
        bills_data.append(sync_data["data"])

    # Return all bills under a single "data" key
    return Response({
        "data": bills_data
    }, status=status.HTTP_200_OK)


@extend_schema(
    summary="Sync Bill to External System",
    description="Send bill data to external system via POST request",
    request=BillSyncRequestSerializer,
    responses={
        200: OpenApiResponse(description="Bill synced successfully"),
        400: OpenApiResponse(description="Sync failed")
    },
    tags=['Tally TCP']
)
@api_view(['POST'])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def vendor_bill_sync_external(request, org_id):
    """Sync bill to external system"""
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
            {'error': 'Bill must be verified before syncing'},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        # Prepare the sync payload
        sync_payload = prepare_sync_data(analyzed_bill, organization)

        # Log the sync attempt
        logger.info(f"Syncing bill {bill_id} for organization {organization.id}")

        # Update bill status to synced
        bill.status = TallyVendorBill.BillStatus.SYNCED
        bill.save(update_fields=['status'])

        # Return success response with the payload that would be sent
        return Response({
            'message': 'Bill synced successfully',
            'bill_id': str(bill_id),
            'status': 'Synced',
            'sync_data': sync_payload
        }, status=status.HTTP_200_OK)

    except Exception as e:
        logger.error(f"Bill sync failed for bill {bill_id}: {str(e)}")
        return Response(
            {'error': f'Sync failed: {str(e)}'},
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
            analyzed_bill.save(force_insert=True)

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


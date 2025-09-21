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
)
from .serializers.common import (
    AnalysisResponseSerializer,
)
from .serializers.vendor_bills import (
    ZohoVendorBillSerializer,
    ZohoVendorBillDetailSerializer,
    VendorZohoBillSerializer,
    ZohoVendorBillUploadSerializer,
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
    Analyze vendor bill content using OpenAI to extract structured data.
    Supports PDF, JPG, PNG file formats.
    """
    logger.info(f"Starting vendor bill analysis for file type: {file_extension}")

    try:
        # Initialize OpenAI client
        api_key = getattr(settings, 'OPENAI_API_KEY', None)
        if not api_key:
            raise ValueError("OpenAI API key not configured in settings")

        client = OpenAI(api_key=api_key)

        # Prepare image data based on file type
        if file_extension.lower() == 'pdf':
            # Convert PDF to image
            images = convert_from_bytes(file_content, first_page=1, last_page=1)
            if not images:
                raise ValueError("Could not convert PDF to image")

            # Convert PIL image to base64
            buffer = BytesIO()
            images[0].save(buffer, format='PNG')
            image_data = base64.b64encode(buffer.getvalue()).decode('utf-8')

        elif file_extension.lower() in ['jpg', 'jpeg', 'png']:
            # Convert image to base64
            image_data = base64.b64encode(file_content).decode('utf-8')

        else:
            raise ValueError(f"Unsupported file format: {file_extension}")

        # JSON Schema for AI extraction
        invoice_schema = {
            "$schema": "http://json-schema.org/draft/2020-12/schema",
            "title": "Invoice",
            "description": "A simple invoice format",
            "type": "object",
            "properties": {
                "invoiceNumber": {"type": "string"},
                "dateIssued": {"type": "string", "format": "date"},
                "dueDate": {"type": "string", "format": "date"},
                "from": {"type": "object", "properties": {"name": {"type": "string"}, "address": {"type": "string"}}},
                "to": {"type": "object", "properties": {"name": {"type": "string"}, "address": {"type": "string"}}},
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

        # Make OpenAI API call
        response = client.chat.completions.create(
            model='gpt-4o',
            response_format={"type": "json_object"},
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text",
                     "text": f"Extract invoice data in JSON format using this schema: {json.dumps(invoice_schema)}"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_data}"}}
                ]
            }],
            max_tokens=1000
        )

        json_data = json.loads(response.choices[0].message.content)
        logger.info(f"Successfully parsed analyzed data: {json_data}")
        return json_data

    except Exception as e:
        logger.error(f"Error in vendor bill analysis: {str(e)}")
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

    bills = VendorBill.objects.filter(organization=organization).order_by('-created_at')

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
    request=ZohoVendorBillUploadSerializer,
    responses=ZohoVendorBillSerializer,
    tags=["Zoho Vendor Bills"],
    methods=["POST"]
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser])
def vendor_bill_upload_view(request, org_id):
    """Upload vendor bill files with PDF splitting support for multiple invoices."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    serializer = ZohoVendorBillUploadSerializer(data=request.data)
    if serializer.is_valid():
        bill_data = serializer.validated_data
        file_type = bill_data.get('fileType', 'Single Invoice/File')
        uploaded_file = bill_data['file']

        # Handle PDF splitting for multiple invoices
        if file_type == 'Multiple Invoice/File' and uploaded_file.name.endswith('.pdf'):
            try:
                uploaded_file.seek(0)
                pdf_bytes = uploaded_file.read()
                pdf = PdfReader(BytesIO(pdf_bytes))

                unique_id = datetime.now().strftime("%Y%m%d%H%M%S")
                created_bills = []

                for page_num in range(len(pdf.pages)):
                    # Convert each PDF page to an image
                    page_images = convert_from_bytes(pdf_bytes, first_page=page_num + 1, last_page=page_num + 1)

                    if page_images:
                        image_io = BytesIO()
                        page_images[0].save(image_io, format='JPEG')
                        image_io.seek(0)

                        # Create separate VendorBill for each page
                        bill = VendorBill.objects.create(
                            billmunshiName=f"BM-Page-{page_num + 1}-{unique_id}",
                            file=ContentFile(image_io.read(), name=f"BM-Page-{page_num + 1}-{unique_id}.jpg"),
                            fileType=file_type,
                            status='Draft',
                            organization=organization
                        )
                        created_bills.append(bill)

                # Return list of created bills with full URLs
                response_serializer = ZohoVendorBillSerializer(created_bills, many=True, context={'request': request})
                return Response({
                    "detail": f"PDF split into {len(created_bills)} bills successfully",
                    "bills": response_serializer.data
                }, status=status.HTTP_201_CREATED)

            except Exception as e:
                logger.error(f"Error processing PDF: {str(e)}")
                return Response({
                    "detail": f"Error processing PDF: {str(e)}"
                }, status=status.HTTP_400_BAD_REQUEST)

        # Restrict PDF uploads for single invoice
        elif file_type == 'Single Invoice/File' and uploaded_file.name.endswith('.pdf'):
            return Response({
                "detail": "PDF upload is not allowed for Single Invoice/File type"
            }, status=status.HTTP_400_BAD_REQUEST)

        # Save regular file upload
        bill = serializer.save(organization=organization, status='Draft')
        response_serializer = ZohoVendorBillSerializer(bill, context={'request': request})
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)

    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


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
    methods=["PUT", "PATCH"]
)
@api_view(['PUT', 'PATCH'])
@permission_classes([IsAuthenticated])
def vendor_bill_verify_view(request, org_id, bill_id):
    """Verify and update Zoho vendor data. Changes status from 'Analyzed' to 'Verified'."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        # Handle the new payload format - extract bill_id and zoho_bill data
        payload_bill_id = request.data.get('bill_id', bill_id)
        zoho_bill_data = request.data.get('zoho_bill', request.data)

        # Use the bill_id from payload if provided, otherwise use URL parameter
        bill = VendorBill.objects.get(id=payload_bill_id, organization=organization)

        if bill.status != 'Analysed':
            return Response(
                {"detail": "Bill must be in 'Analysed' status to verify"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Get existing VendorZohoBill
        try:
            zoho_bill = VendorZohoBill.objects.get(selectBill=bill, organization=organization)
        except VendorZohoBill.DoesNotExist:
            return Response(
                {"detail": "No analyzed vendor data found. Please analyze the bill first."},
                status=status.HTTP_400_BAD_REQUEST
            )

        with transaction.atomic():
            partial = request.method == 'PATCH'
            serializer = VendorZohoBillSerializer(zoho_bill, data=zoho_bill_data, partial=partial)

            if serializer.is_valid():
                updated_bill = serializer.save()

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
                bill.status = 'Verified'
                bill.save()

                return Response(VendorZohoBillSerializer(updated_bill).data)

            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    except VendorBill.DoesNotExist:
        return Response({"detail": "Vendor bill not found"}, status=status.HTTP_404_NOT_FOUND)


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

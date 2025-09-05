# apps/module/zoho/views.py

import json
import base64
import logging
from io import BytesIO
from typing import List, Dict, Any

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone

import requests
from pdf2image import convert_from_bytes

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, parser_classes
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination
from drf_spectacular.utils import extend_schema

from apps.organizations.models import Organization
from apps.common.pagination import DefaultPagination
from .models import (
    ZohoCredentials,
    ZohoVendor,
    ZohoChartOfAccount,
    ZohoTaxes,
    ZohoTdsTcs,
    ZohoVendorCredit,
    VendorBill,
    VendorZohoBill,
    VendorZohoProduct,
    ExpenseBill,
    ExpenseZohoBill,
    ExpenseZohoProduct,
)
from .serializers.settings import (
    ZohoCredentialsSerializer,
    ZohoVendorSerializer,
    ZohoChartOfAccountSerializer,
    ZohoTaxesSerializer,
    ZohoTdsTcsSerializer,
)
from .serializers.vendor_bills import (
    ZohoVendorBillSerializer,
    ZohoVendorBillDetailSerializer,
    VendorZohoBillSerializer,
    ZohoVendorBillUploadSerializer,
    ZohoSyncResultSerializer,
    ZohoAnalysisResultSerializer,
    ZohoOperationResultSerializer,
)
from .serializers.expense_bills import (
    ZohoExpenseBillSerializer,
    ZohoExpenseBillDetailSerializer,
    ExpenseZohoBillSerializer,
    ZohoExpenseBillUploadSerializer,
)
from .serializers.common import (
    TokenResponseSerializer,
    SyncResponseSerializer,
    AnalysisResponseSerializer,
    ZohoSyncResponseSerializer,
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


def create_vendor_zoho_objects_from_analysis(bill, analyzed_data, organization):
    """
    Create VendorZohoBill and VendorZohoProduct objects from analyzed data.
    This allows users to proceed to the verification step.
    """
    from datetime import datetime

    logger.info(f"Creating Zoho objects for bill {bill.id} with analyzed data: {analyzed_data}")

    # Try to find vendor by GST number or name
    vendor = None
    if analyzed_data.get('vendorGST'):
        vendor = ZohoVendor.objects.filter(
            organization=organization,
            gstNo=analyzed_data['vendorGST']
        ).first()
        logger.info(f"Found vendor by GST {analyzed_data['vendorGST']}: {vendor}")

    if not vendor and analyzed_data.get('vendorName'):
        vendor = ZohoVendor.objects.filter(
            organization=organization,
            companyName__icontains=analyzed_data['vendorName']
        ).first()
        logger.info(f"Found vendor by name {analyzed_data['vendorName']}: {vendor}")

    # Parse date with multiple format support
    bill_date = None
    if analyzed_data.get('dateIssued'):
        date_formats = ['%Y-%m-%d', '%d-%m-%Y', '%m/%d/%Y', '%d/%m/%Y']
        for date_format in date_formats:
            try:
                bill_date = datetime.strptime(analyzed_data['dateIssued'], date_format).date()
                break
            except (ValueError, TypeError):
                continue

        if not bill_date:
            logger.warning(f"Could not parse date: {analyzed_data.get('dateIssued')}")

    # Validate numeric fields and convert to string
    def safe_numeric_string(value, default='0'):
        try:
            if value is None:
                return default
            # Handle both int/float and string inputs
            if isinstance(value, (int, float)):
                return str(value)
            # If it's already a string, validate it's numeric
            float(str(value))  # This will raise ValueError if not numeric
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
                'bill_no': analyzed_data.get('invoiceNumber', ''),
                'bill_date': bill_date,
                'total': safe_numeric_string(analyzed_data.get('total')),
                'igst': safe_numeric_string(analyzed_data.get('igst')),
                'cgst': safe_numeric_string(analyzed_data.get('cgst')),
                'sgst': safe_numeric_string(analyzed_data.get('sgst')),
                'note': f"Auto-created from analysis for {analyzed_data.get('vendorName', 'Unknown Vendor')}"
            }
        )

        if created:
            logger.info(f"Created new VendorZohoBill: {zoho_bill.id}")
        else:
            logger.info(f"Found existing VendorZohoBill: {zoho_bill.id}")
            # Update the existing bill with new analyzed data
            zoho_bill.vendor = vendor
            zoho_bill.bill_no = analyzed_data.get('invoiceNumber', zoho_bill.bill_no)
            zoho_bill.bill_date = bill_date or zoho_bill.bill_date
            zoho_bill.total = safe_numeric_string(analyzed_data.get('total'), zoho_bill.total)
            zoho_bill.igst = safe_numeric_string(analyzed_data.get('igst'), zoho_bill.igst)
            zoho_bill.cgst = safe_numeric_string(analyzed_data.get('cgst'), zoho_bill.cgst)
            zoho_bill.sgst = safe_numeric_string(analyzed_data.get('sgst'), zoho_bill.sgst)
            zoho_bill.note = f"Updated from analysis for {analyzed_data.get('vendorName', 'Unknown Vendor')}"
            zoho_bill.save()
            logger.info(f"Updated existing VendorZohoBill: {zoho_bill.id}")

        # Delete existing products and recreate them
        existing_products_count = zoho_bill.products.count()
        if existing_products_count > 0:
            zoho_bill.products.all().delete()
            logger.info(f"Deleted {existing_products_count} existing products")

        # Create VendorZohoProduct objects for each item
        items = analyzed_data.get('items', [])
        logger.info(f"Creating {len(items)} product line items")

        created_products = []
        for idx, item in enumerate(items):
            try:
                product = VendorZohoProduct.objects.create(
                    zohoBill=zoho_bill,
                    organization=organization,
                    item_name=item.get('description', f'Item {idx + 1}')[:100],  # Truncate to field limit
                    item_details=item.get('description', f'Item {idx + 1}')[:200],  # Truncate to field limit
                    rate=safe_numeric_string(item.get('rate')),
                    quantity=safe_numeric_string(item.get('quantity'), '1'),
                    amount=safe_numeric_string(item.get('amount'))
                )
                created_products.append(product)
                logger.info(f"Created product {idx + 1}: {product.item_name} - Rate: {product.rate}, Qty: {product.quantity}, Amount: {product.amount}")
            except Exception as e:
                logger.error(f"Error creating product {idx + 1}: {str(e)}")
                # Continue with other products even if one fails

        logger.info(f"Successfully created {len(created_products)} products for bill {zoho_bill.id}")

        # Validate that the sum of product amounts matches the subtotal (if provided)
        if analyzed_data.get('subtotal'):
            try:
                expected_subtotal = float(analyzed_data['subtotal'])
                actual_subtotal = sum(float(p.amount) for p in created_products if p.amount)
                if abs(expected_subtotal - actual_subtotal) > 0.01:  # Allow small rounding differences
                    logger.warning(f"Subtotal mismatch: expected {expected_subtotal}, got {actual_subtotal}")
            except (ValueError, TypeError) as e:
                logger.warning(f"Could not validate subtotal: {str(e)}")

        return zoho_bill

    except Exception as e:
        logger.error(f"Error creating Zoho objects for bill {bill.id}: {str(e)}")
        raise

def analyze_bill_with_openai(file_content, file_extension):
    """
    Analyze bill content using OpenAI to extract structured data.
    Supports PDF, JPG, PNG file formats.
    """
    import openai
    import base64
    from pdf2image import convert_from_bytes
    from io import BytesIO
    from django.conf import settings

    logger.info(f"Starting bill analysis for file type: {file_extension}")

    try:
        # Configure OpenAI
        openai.api_key = getattr(settings, 'OPENAI_API_KEY', None)
        if not openai.api_key:
            raise ValueError("OpenAI API key not configured in settings")

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

        # Prepare OpenAI prompt for bill analysis
        prompt = """
        Analyze this invoice/bill image and extract the following information in JSON format:
        
        {
          "invoiceNumber": "Invoice/Bill number",
          "dateIssued": "Date in YYYY-MM-DD format",
          "vendorName": "Vendor/Company name",
          "vendorGST": "GST number if available",
          "items": [
            {
              "description": "Item description",
              "quantity": "Quantity as number",
              "rate": "Rate per unit as number",
              "amount": "Total amount for this item as number"
            }
          ],
          "subtotal": "Subtotal amount as number",
          "igst": "IGST amount as number (0 if not applicable)",
          "cgst": "CGST amount as number (0 if not applicable)", 
          "sgst": "SGST amount as number (0 if not applicable)",
          "total": "Total amount as number"
        }
        
        Important notes:
        - Extract all numerical values as numbers, not strings
        - If IGST is present, CGST and SGST should be 0
        - If CGST and SGST are present, IGST should be 0
        - Ensure all amounts are accurate and match the bill
        - If any field is not found, use appropriate defaults (empty string for text, 0 for numbers)
        """

        # Make OpenAI API call
        response = openai.ChatCompletion.create(
            model="gpt-4-vision-preview",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{image_data}",
                                "detail": "high"
                            }
                        }
                    ]
                }
            ],
            max_tokens=2000,
            temperature=0.1
        )

        # Extract and parse response
        content = response.choices[0].message.content
        logger.info(f"OpenAI response: {content}")

        # Try to extract JSON from the response
        import json
        import re

        # Look for JSON content between ```json and ``` or just parse the whole content
        json_match = re.search(r'```json\s*(.*?)\s*```', content, re.DOTALL)
        if json_match:
            json_content = json_match.group(1)
        else:
            # Try to find JSON object in the content
            json_match = re.search(r'\{.*\}', content, re.DOTALL)
            if json_match:
                json_content = json_match.group(0)
            else:
                json_content = content

        try:
            analyzed_data = json.loads(json_content)
            logger.info(f"Successfully parsed analyzed data: {analyzed_data}")
            return analyzed_data

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON response: {e}")
            logger.error(f"Raw content: {content}")
            # Return a default structure if parsing fails
            return {
                "invoiceNumber": "",
                "dateIssued": "",
                "vendorName": "",
                "vendorGST": "",
                "items": [],
                "subtotal": 0,
                "igst": 0,
                "cgst": 0,
                "sgst": 0,
                "total": 0,
                "error": f"Failed to parse OpenAI response: {str(e)}"
            }

    except Exception as e:
        logger.error(f"Error in bill analysis: {str(e)}")
        return {
            "invoiceNumber": "",
            "dateIssued": "",
            "vendorName": "",
            "vendorGST": "",
            "items": [],
            "subtotal": 0,
            "igst": 0,
            "cgst": 0,
            "sgst": 0,
            "total": 0,
            "error": f"Analysis failed: {str(e)}"
        }


# ============================================================================
# Zoho Settings/Credentials Management
# ============================================================================

@extend_schema(
    responses=ZohoCredentialsSerializer,
    tags=["Zoho Ops"],
    methods=["GET"]
)
@extend_schema(
    request=ZohoCredentialsSerializer,
    responses=ZohoCredentialsSerializer,
    tags=["Zoho Ops"],
    methods=["PUT", "PATCH"]
)
@api_view(['GET', 'PUT', 'PATCH'])
@permission_classes([IsAuthenticated])
def zoho_credentials_view(request, org_id):
    """Get or update Zoho credentials for the organization."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        credentials = ZohoCredentials.objects.get(organization=organization)
    except ZohoCredentials.DoesNotExist:
        if request.method == 'GET':
            return Response({"detail": "Zoho credentials not found"}, status=status.HTTP_404_NOT_FOUND)
        # Create new credentials for PUT/PATCH
        credentials = None

    if request.method == 'GET':
        serializer = ZohoCredentialsSerializer(credentials)
        return Response(serializer.data)

    elif request.method in ['PUT', 'PATCH']:
        partial = request.method == 'PATCH'
        if credentials:
            serializer = ZohoCredentialsSerializer(credentials, data=request.data, partial=partial)
        else:
            serializer = ZohoCredentialsSerializer(data=request.data)

        if serializer.is_valid():
            serializer.save(organization=organization)
            return Response(serializer.data)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)



@extend_schema(
    responses={"200": {"access_token": "string", "refresh_token": "string", "expires_in": "integer"}},
    tags=["Zoho Ops"],
    methods=["POST"]
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def generate_token_view(request):
    """Generate access and refresh tokens using the access code from Zoho OAuth."""
    organization = get_organization_from_request(request)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        credentials = ZohoCredentials.objects.get(organization=organization)
    except ZohoCredentials.DoesNotExist:
        return Response(
            {"detail": "Zoho credentials not found. Please configure credentials first."},
            status=status.HTTP_404_NOT_FOUND
        )

    if not credentials.accessCode or credentials.accessCode == "Your Access Code":
        return Response(
            {"detail": "Access code not provided. Please set the access code first."},
            status=status.HTTP_400_BAD_REQUEST
        )

    # Prepare token generation request
    token_url = "https://accounts.zoho.in/oauth/v2/token"

    token_data = {
        'code': credentials.accessCode,
        'client_id': credentials.clientId,
        'client_secret': credentials.clientSecret,
        'redirect_uri': credentials.redirectUrl,
        'grant_type': 'authorization_code'
    }

    try:
        # Make request to Zoho OAuth API
        response = requests.post(token_url, data=token_data, timeout=30)

        if response.status_code == 200:
            token_response = response.json()

            # Update credentials with new tokens
            credentials.accessToken = token_response.get('access_token')
            credentials.refreshToken = token_response.get('refresh_token')

            # Set token expiry (Zoho tokens typically last 1 hour)
            expires_in = token_response.get('expires_in', 3600)
            credentials.token_expiry = timezone.now() + timezone.timedelta(seconds=expires_in)

            credentials.save(update_fields=['accessToken', 'refreshToken', 'token_expiry', 'update_at'])

            return Response({
                "detail": "Tokens generated successfully",
                "access_token": credentials.accessToken,
                "refresh_token": credentials.refreshToken,
                "expires_in": expires_in,
                "token_expiry": credentials.token_expiry
            })

        else:
            error_data = response.json() if response.content else {}
            logger.error(f"Zoho token generation failed: {response.status_code} - {response.text}")

            return Response({
                "detail": f"Token generation failed: {error_data.get('error_description', 'Unknown error')}",
                "error_code": error_data.get('error', 'token_generation_failed')
            }, status=status.HTTP_400_BAD_REQUEST)

    except requests.RequestException as e:
        logger.error(f"Network error during token generation: {str(e)}")
        return Response(
            {"detail": f"Network error: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
    except Exception as e:
        logger.error(f"Unexpected error during token generation: {str(e)}")
        return Response(
            {"detail": f"Unexpected error: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


# ============================================================================
# Zoho Sync Endpoints (GET & SYNC only)
# ============================================================================

@extend_schema(
    responses=ZohoVendorSerializer(many=True),
    tags=["Zoho Ops"],
    methods=["GET"]
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def vendors_list_view(request, org_id):
    """List all vendors for the organization with pagination."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    vendors = ZohoVendor.objects.filter(organization=organization).order_by('companyName')

    # Apply pagination
    paginator = DefaultPagination()
    paginated_vendors = paginator.paginate_queryset(vendors, request)

    if paginated_vendors is not None:
        serializer = ZohoVendorSerializer(paginated_vendors, many=True)
        return paginator.get_paginated_response(serializer.data)

    # Fallback if pagination fails
    serializer = ZohoVendorSerializer(vendors, many=True)
    return Response({
        "count": vendors.count(),
        "next": None,
        "previous": None,
        "results": serializer.data
    })


@extend_schema(
    responses={"200": {"detail": "Vendors synced successfully"}},
    tags=["Zoho Ops"],
    methods=["POST"]
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def vendors_sync_view(request):
    """Sync vendors from Zoho Books."""
    organization = get_organization_from_request(request)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        credentials = get_zoho_credentials(organization)
        zoho_data = make_zoho_api_request(credentials, "contacts")

        synced_count = 0
        for contact in zoho_data.get('contacts', []):
            if contact.get('contact_type') == 'vendor':
                vendor, created = ZohoVendor.objects.update_or_create(
                    organization=organization,
                    contactId=contact['contact_id'],
                    defaults={
                        'companyName': contact.get('company_name', ''),
                        'gstNo': contact.get('gst_no', '')
                    }
                )
                if created:
                    synced_count += 1

        return Response({
            "detail": f"Successfully synced {synced_count} vendors",
            "synced_count": synced_count
        })
    except Exception as e:
        return Response(
            {"detail": f"Sync failed: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@extend_schema(
    responses=ZohoChartOfAccountSerializer(many=True),
    tags=["Zoho Ops"],
    methods=["GET"]
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def chart_of_accounts_list_view(request, org_id):
    """List all chart of accounts for the organization with pagination."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    accounts = ZohoChartOfAccount.objects.filter(organization=organization).order_by('accountName')

    # Apply pagination
    paginator = DefaultPagination()
    paginated_accounts = paginator.paginate_queryset(accounts, request)

    if paginated_accounts is not None:
        serializer = ZohoChartOfAccountSerializer(paginated_accounts, many=True)
        return paginator.get_paginated_response(serializer.data)

    # Fallback if pagination fails
    serializer = ZohoChartOfAccountSerializer(accounts, many=True)
    return Response({"results": serializer.data})


@extend_schema(
    responses={"200": {"detail": "Chart of accounts synced successfully"}},
    tags=["Zoho Ops"],
    methods=["POST"]
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def chart_of_accounts_sync_view(request):
    """Sync chart of accounts from Zoho Books."""
    organization = get_organization_from_request(request)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        credentials = get_zoho_credentials(organization)
        zoho_data = make_zoho_api_request(credentials, "chartofaccounts")

        synced_count = 0
        for account in zoho_data.get('chartofaccounts', []):
            chart_account, created = ZohoChartOfAccount.objects.update_or_create(
                organization=organization,
                accountId=account['account_id'],
                defaults={
                    'accountName': account.get('account_name', '')
                }
            )
            if created:
                synced_count += 1

        return Response({
            "detail": f"Successfully synced {synced_count} chart of accounts",
            "synced_count": synced_count
        })
    except Exception as e:
        return Response(
            {"detail": f"Sync failed: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@extend_schema(
    responses=ZohoTaxesSerializer(many=True),
    tags=["Zoho Ops"],
    methods=["GET"]
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def taxes_list_view(request, org_id):
    """List all taxes for the organization with pagination."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    taxes = ZohoTaxes.objects.filter(organization=organization).order_by('taxName')

    # Apply pagination
    paginator = DefaultPagination()
    paginated_taxes = paginator.paginate_queryset(taxes, request)

    if paginated_taxes is not None:
        serializer = ZohoTaxesSerializer(paginated_taxes, many=True)
        return paginator.get_paginated_response(serializer.data)

    # Fallback if pagination fails
    serializer = ZohoTaxesSerializer(taxes, many=True)
    return Response({"results": serializer.data})


@extend_schema(
    responses={"200": {"detail": "Taxes synced successfully"}},
    tags=["Zoho Ops"],
    methods=["POST"]
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def taxes_sync_view(request):
    """Sync taxes from Zoho Books."""
    organization = get_organization_from_request(request)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        credentials = get_zoho_credentials(organization)
        zoho_data = make_zoho_api_request(credentials, "taxes")

        synced_count = 0
        for tax in zoho_data.get('taxes', []):
            tax_obj, created = ZohoTaxes.objects.update_or_create(
                organization=organization,
                taxId=tax['tax_id'],
                defaults={
                    'taxName': tax.get('tax_name', '')
                }
            )
            if created:
                synced_count += 1

        return Response({
            "detail": f"Successfully synced {synced_count} taxes",
            "synced_count": synced_count
        })
    except Exception as e:
        return Response(
            {"detail": f"Sync failed: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@extend_schema(
    responses=ZohoTdsTcsSerializer(many=True),
    tags=["Zoho Ops"],
    methods=["GET"]
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def tds_tcs_list_view(request, org_id):
    """List all TDS/TCS for the organization with pagination."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    tds_tcs = ZohoTdsTcs.objects.filter(organization=organization).order_by('taxName')

    # Apply pagination
    paginator = DefaultPagination()
    paginated_tds_tcs = paginator.paginate_queryset(tds_tcs, request)

    if paginated_tds_tcs is not None:
        serializer = ZohoTdsTcsSerializer(paginated_tds_tcs, many=True)
        return paginator.get_paginated_response(serializer.data)

    # Fallback if pagination fails
    serializer = ZohoTdsTcsSerializer(tds_tcs, many=True)
    return Response({
        "count": tds_tcs.count(),
        "next": None,
        "previous": None,
        "results": serializer.data
    })


# ============================================================================
# Vendor Bills Workflow (Draft → Analyzed → Verified → Synced)
# ============================================================================

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
        serializer = ZohoVendorBillSerializer(paginated_bills, many=True)
        return paginator.get_paginated_response(serializer.data)

    # Fallback if pagination fails
    serializer = ZohoVendorBillSerializer(bills, many=True)
    return Response({"results": serializer.data})


@extend_schema(
    request=ZohoVendorBillUploadSerializer,
    responses=ZohoVendorBillSerializer,
    tags=["Zoho Vendor Bills"],
    methods=["POST"]
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser])
def vendor_bills_upload_view(request, org_id):
    """Upload vendor bill files (JPG, PNG, PDF). Status starts as 'Draft'."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    serializer = ZohoVendorBillUploadSerializer(data=request.data)
    if serializer.is_valid():
        bill = serializer.save(organization=organization, status='Draft')
        response_serializer = ZohoVendorBillSerializer(bill)
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)

    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


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
        bill = VendorBill.objects.get(id=bill_id, organization=organization)
        serializer = ZohoVendorBillDetailSerializer(bill)
        return Response(serializer.data)
    except VendorBill.DoesNotExist:
        return Response({"detail": "Vendor bill not found"}, status=status.HTTP_404_NOT_FOUND)


@extend_schema(
    responses=AnalysisResponseSerializer,
    tags=["Zoho Vendor Bills"],
    methods=["POST"]
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def vendor_bill_analyze_view(request, bill_id):
    """Analyze vendor bill using OpenAI. Changes status from 'Draft' to 'Analyzed'."""
    organization = get_organization_from_request(request)
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
        bill.file.seek(0)
        file_content = bill.file.read()
        file_extension = bill.file.name.split('.')[-1].lower()

        # Analyze with OpenAI
        analyzed_data = analyze_bill_with_openai(file_content, file_extension)

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
    """Verify and create Zoho bill data. Changes status from 'Analyzed' to 'Verified'."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        bill = VendorBill.objects.get(id=bill_id, organization=organization)

        if bill.status != 'Analysed':
            return Response(
                {"detail": "Bill must be in 'Analysed' status to verify"},
                status=status.HTTP_400_BAD_REQUEST
            )

        with transaction.atomic():
            # Create Zoho bill data
            serializer = VendorZohoBillSerializer(data=request.data)
            if serializer.is_valid():
                zoho_bill = serializer.save(
                    organization=organization,
                    selectBill=bill
                )

                # Update bill status
                bill.status = 'Verified'
                bill.save()

                return Response(serializer.data, status=status.HTTP_201_CREATED)

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
def vendor_bill_sync_view(request, bill_id):
    """Sync verified vendor bill to Zoho Books. Changes status to 'Synced'."""
    organization = get_organization_from_request(request)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        bill = VendorBill.objects.get(id=bill_id, organization=organization)

        if bill.status != 'Verified':
            return Response(
                {"detail": "Bill must be in 'Verified' status to sync"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Get Zoho bill data
        try:
            zoho_bill = VendorZohoBill.objects.get(selectBill=bill)
        except VendorZohoBill.DoesNotExist:
            return Response(
                {"detail": "Zoho bill data not found. Please verify the bill first."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Prepare data for Zoho API
        zoho_data = {
            "vendor_id": zoho_bill.vendor.contactId if zoho_bill.vendor else None,
            "bill_number": zoho_bill.bill_no,
            "date": str(zoho_bill.bill_date) if zoho_bill.bill_date else None,
            "line_items": [],
            "notes": zoho_bill.note
        }

        # Add line items
        for product in zoho_bill.products.all():
            item_data = {
                "account_id": product.chart_of_accounts.accountId if product.chart_of_accounts else None,
                "name": product.item_name,
                "description": product.item_details,
                "rate": float(product.rate) if product.rate else 0,
                "quantity": float(product.quantity) if product.quantity else 1,
                "tax_id": product.taxes.taxId if product.taxes else None
            }
            zoho_data["line_items"].append(item_data)

        # Sync to Zoho Books
        credentials = get_zoho_credentials(organization)
        result = make_zoho_api_request(credentials, "bills", method='POST', data=zoho_data)

        # Update bill status
        bill.status = 'Synced'
        bill.save()

        return Response({
            "detail": "Bill synced to Zoho successfully",
            "zoho_bill_id": result.get('bill', {}).get('bill_id')
        })

    except VendorBill.DoesNotExist:
        return Response({"detail": "Vendor bill not found"}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        return Response(
            {"detail": f"Sync failed: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


# ============================================================================
# Expense Bills Workflow (Draft → Analyzed → Verified → Synced)
# ============================================================================

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

    bills = ExpenseBill.objects.filter(organization=organization).order_by('-created_at')

    # Apply pagination
    paginator = DefaultPagination()
    paginated_bills = paginator.paginate_queryset(bills, request)

    if paginated_bills is not None:
        serializer = ZohoExpenseBillSerializer(paginated_bills, many=True)
        return paginator.get_paginated_response(serializer.data)

    # Fallback if pagination fails
    serializer = ZohoExpenseBillSerializer(bills, many=True)
    return Response({"results": serializer.data})


@extend_schema(
    request=ZohoExpenseBillUploadSerializer,
    responses=ZohoExpenseBillSerializer,
    tags=["Zoho Expense Bills"],
    methods=["POST"]
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser])
def expense_bills_upload_view(request, org_id):
    """Upload expense bill files (JPG, PNG, PDF). Status starts as 'Draft'."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    serializer = ZohoExpenseBillUploadSerializer(data=request.data)
    if serializer.is_valid():
        bill = serializer.save(organization=organization, status='Draft')
        response_serializer = ZohoExpenseBillSerializer(bill)
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)

    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


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
        bill = ExpenseBill.objects.get(id=bill_id, organization=organization)
        serializer = ZohoExpenseBillDetailSerializer(bill)
        return Response(serializer.data)
    except ExpenseBill.DoesNotExist:
        return Response({"detail": "Expense bill not found"}, status=status.HTTP_404_NOT_FOUND)


@extend_schema(
    responses=ZohoAnalysisResultSerializer,
    tags=["Zoho Expense Bills"],
    methods=["POST"]
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def expense_bill_analyze_view(request, bill_id):
    """Analyze expense bill using OpenAI. Changes status from 'Draft' to 'Analyzed'."""
    organization = get_organization_from_request(request)
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
        bill.file.seek(0)
        file_content = bill.file.read()
        file_extension = bill.file.name.split('.')[-1].lower()

        # Analyze with OpenAI
        analyzed_data = analyze_bill_with_openai(file_content, file_extension)

        # Update bill with analyzed data
        bill.analysed_data = analyzed_data
        bill.status = 'Analysed'
        bill.save()

        return Response({
            "detail": "Bill analyzed successfully",
            "analyzed_data": analyzed_data
        })

    except ExpenseBill.DoesNotExist:
        return Response({"detail": "Expense bill not found"}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        return Response(
            {"detail": f"Analysis failed: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@extend_schema(
    request=ExpenseZohoBillSerializer,
    responses=ExpenseZohoBillSerializer,
    tags=["Zoho Expense Bills"],
    methods=["POST"]
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def expense_bill_verify_view(request, org_id, bill_id):
    """Verify and create Zoho expense data. Changes status from 'Analyzed' to 'Verified'."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        bill = ExpenseBill.objects.get(id=bill_id, organization=organization)

        if bill.status != 'Analysed':
            return Response(
                {"detail": "Bill must be in 'Analysed' status to verify"},
                status=status.HTTP_400_BAD_REQUEST
            )

        with transaction.atomic():
            # Create Zoho expense data
            serializer = ExpenseZohoBillSerializer(data=request.data)
            if serializer.is_valid():
                zoho_bill = serializer.save(
                    organization=organization,
                    selectBill=bill
                )

                # Update bill status
                bill.status = 'Verified'
                bill.save()

                return Response(serializer.data, status=status.HTTP_201_CREATED)

            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    except ExpenseBill.DoesNotExist:
        return Response({"detail": "Expense bill not found"}, status=status.HTTP_404_NOT_FOUND)


@extend_schema(
    responses={"200": {"detail": "Expense synced to Zoho successfully"}},
    tags=["Zoho Expense Bills"],
    methods=["POST"]
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def expense_bill_sync_view(request, bill_id):
    """Sync verified expense bill to Zoho Books. Changes status to 'Synced'."""
    organization = get_organization_from_request(request)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        bill = ExpenseBill.objects.get(id=bill_id, organization=organization)

        if bill.status != 'Verified':
            return Response(
                {"detail": "Bill must be in 'Verified' status to sync"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Get Zoho expense data
        try:
            zoho_bill = ExpenseZohoBill.objects.get(selectBill=bill)
        except ExpenseZohoBill.DoesNotExist:
            return Response(
                {"detail": "Zoho expense data not found. Please verify the bill first."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Prepare data for Zoho API
        zoho_data = {
            "account_id": zoho_bill.products.first().chart_of_accounts.accountId if zoho_bill.products.exists() else None,
            "date": str(zoho_bill.bill_date) if zoho_bill.bill_date else None,
            "amount": float(zoho_bill.total) if zoho_bill.total else 0,
            "description": zoho_bill.note,
            "vendor_id": zoho_bill.vendor.contactId if zoho_bill.vendor else None
        }

        # Sync to Zoho Books
        credentials = get_zoho_credentials(organization)
        result = make_zoho_api_request(credentials, "expenses", method='POST', data=zoho_data)

        # Update bill status
        bill.status = 'Synced'
        bill.save()

        return Response({
            "detail": "Expense synced to Zoho successfully",
            "zoho_expense_id": result.get('expense', {}).get('expense_id')
        })

    except ExpenseBill.DoesNotExist:
        return Response({"detail": "Expense bill not found"}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        return Response(
            {"detail": f"Sync failed: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


# ============================================================================
# Sync endpoints with explicit serializer responses
# ============================================================================

@extend_schema(
    responses=ZohoSyncResultSerializer,
    tags=["Zoho Ops"],
    methods=["POST"]
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def vendors_sync_view(request):
    """Sync vendors from Zoho Books."""
    organization = get_organization_from_request(request)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        credentials = get_zoho_credentials(organization)
        zoho_data = make_zoho_api_request(credentials, "contacts")

        synced_count = 0
        for contact in zoho_data.get('contacts', []):
            if contact.get('contact_type') == 'vendor':
                vendor, created = ZohoVendor.objects.update_or_create(
                    organization=organization,
                    contactId=contact['contact_id'],
                    defaults={
                        'companyName': contact.get('company_name', ''),
                        'gstNo': contact.get('gst_no', '')
                    }
                )
                if created:
                    synced_count += 1

        return Response({
            "detail": f"Successfully synced {synced_count} vendors",
            "synced_count": synced_count
        })
    except Exception as e:
        return Response(
            {"detail": f"Sync failed: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@extend_schema(
    responses=ZohoSyncResultSerializer,
    tags=["Zoho Ops"],
    methods=["POST"]
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def chart_of_accounts_sync_view(request):
    """Sync chart of accounts from Zoho Books."""
    organization = get_organization_from_request(request)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        credentials = get_zoho_credentials(organization)
        zoho_data = make_zoho_api_request(credentials, "chartofaccounts")

        synced_count = 0
        for account in zoho_data.get('chartofaccounts', []):
            chart_account, created = ZohoChartOfAccount.objects.update_or_create(
                organization=organization,
                accountId=account['account_id'],
                defaults={
                    'accountName': account.get('account_name', '')
                }
            )
            if created:
                synced_count += 1

        return Response({
            "detail": f"Successfully synced {synced_count} chart of accounts",
            "synced_count": synced_count
        })
    except Exception as e:
        return Response(
            {"detail": f"Sync failed: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@extend_schema(
    responses=ZohoSyncResultSerializer,
    tags=["Zoho Ops"],
    methods=["POST"]
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def taxes_sync_view(request):
    """Sync taxes from Zoho Books."""
    organization = get_organization_from_request(request)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        credentials = get_zoho_credentials(organization)
        zoho_data = make_zoho_api_request(credentials, "taxes")

        synced_count = 0
        for tax in zoho_data.get('taxes', []):
            tax_obj, created = ZohoTaxes.objects.update_or_create(
                organization=organization,
                taxId=tax['tax_id'],
                defaults={
                    'taxName': tax.get('tax_name', '')
                }
            )
            if created:
                synced_count += 1

        return Response({
            "detail": f"Successfully synced {synced_count} taxes",
            "synced_count": synced_count
        })
    except Exception as e:
        return Response(
            {"detail": f"Sync failed: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@extend_schema(
    responses=ZohoSyncResultSerializer,
    tags=["Zoho Ops"],
    methods=["POST"]
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def tds_tcs_sync_view(request):
    """Sync TDS/TCS from Zoho Books."""
    organization = get_organization_from_request(request)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        credentials = get_zoho_credentials(organization)

        # Sync TDS taxes
        tds_data = make_zoho_api_request(credentials, "taxes?tax_type=tds")
        tds_count = 0
        for tax in tds_data.get('taxes', []):
            tds_obj, created = ZohoTdsTcs.objects.update_or_create(
                organization=organization,
                taxId=tax['tax_id'],
                defaults={
                    'taxName': tax.get('tax_name', ''),
                    'taxPercentage': str(tax.get('tax_percentage', 0)),
                    'taxType': 'TDS'
                }
            )
            if created:
                tds_count += 1

        # Sync TCS taxes
        tcs_data = make_zoho_api_request(credentials, "taxes?tax_type=tcs")
        tcs_count = 0
        for tax in tcs_data.get('taxes', []):
            tcs_obj, created = ZohoTdsTcs.objects.update_or_create(
                organization=organization,
                taxId=tax['tax_id'],
                defaults={
                    'taxName': tax.get('tax_name', ''),
                    'taxPercentage': str(tax.get('tax_percentage', 0)),
                    'taxType': 'TCS'
                }
            )
            if created:
                tcs_count += 1

        total_synced = tds_count + tcs_count
        return Response({
            "detail": f"Successfully synced {total_synced} TDS/TCS ({tds_count} TDS, {tcs_count} TCS)",
            "synced_count": total_synced
        })
    except Exception as e:
        return Response(
            {"detail": f"Sync failed: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@extend_schema(
    responses=ZohoOperationResultSerializer,
    tags=["Zoho Ops"],
    methods=["POST"]
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def generate_token_view(request):
    """Generate access and refresh tokens using the access code from Zoho OAuth."""
    organization = get_organization_from_request(request)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        credentials = ZohoCredentials.objects.get(organization=organization)
    except ZohoCredentials.DoesNotExist:
        return Response(
            {"detail": "Zoho credentials not found. Please configure credentials first."},
            status=status.HTTP_404_NOT_FOUND
        )

    if not credentials.accessCode or credentials.accessCode == "Your Access Code":
        return Response(
            {"detail": "Access code not provided. Please set the access code first."},
            status=status.HTTP_400_BAD_REQUEST
        )

    # Prepare token generation request
    token_url = "https://accounts.zoho.in/oauth/v2/token"

    token_data = {
        'code': credentials.accessCode,
        'client_id': credentials.clientId,
        'client_secret': credentials.clientSecret,
        'redirect_uri': credentials.redirectUrl,
        'grant_type': 'authorization_code'
    }

    try:
        # Make request to Zoho OAuth API
        response = requests.post(token_url, data=token_data, timeout=30)

        if response.status_code == 200:
            token_response = response.json()

            # Update credentials with new tokens
            credentials.accessToken = token_response.get('access_token')
            credentials.refreshToken = token_response.get('refresh_token')

            # Set token expiry (Zoho tokens typically last 1 hour)
            expires_in = token_response.get('expires_in', 3600)
            credentials.token_expiry = timezone.now() + timezone.timedelta(seconds=expires_in)

            credentials.save(update_fields=['accessToken', 'refreshToken', 'token_expiry', 'update_at'])

            return Response({
                "detail": "Tokens generated successfully",
                "access_token": credentials.accessToken,
                "refresh_token": credentials.refreshToken,
                "expires_in": expires_in,
                "token_expiry": credentials.token_expiry
            })

        else:
            error_data = response.json() if response.content else {}
            logger.error(f"Zoho token generation failed: {response.status_code} - {response.text}")

            return Response({
                "detail": f"Token generation failed: {error_data.get('error_description', 'Unknown error')}",
                "error_code": error_data.get('error', 'token_generation_failed')
            }, status=status.HTTP_400_BAD_REQUEST)

    except requests.RequestException as e:
        logger.error(f"Network error during token generation: {str(e)}")
        return Response(
            {"detail": f"Network error: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
    except Exception as e:
        logger.error(f"Unexpected error during token generation: {str(e)}")
        return Response(
            {"detail": f"Unexpected error: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )

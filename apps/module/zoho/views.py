# apps/module/zoho/views.py

import logging
import requests

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from drf_spectacular.utils import extend_schema

from apps.organizations.models import Organization
from apps.common.pagination import DefaultPagination
from .models import (
    ZohoCredentials,
    ZohoVendor,
    ZohoChartOfAccount,
    ZohoTaxes,
    ZohoTdsTcs,
)
from .serializers.settings import (
    ZohoCredentialsSerializer,
    ZohoVendorSerializer,
    ZohoChartOfAccountSerializer,
    ZohoTaxesSerializer,
    ZohoTdsTcsSerializer,
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
def generate_token_view(request, org_id):
    """Generate access and refresh tokens using the access code from Zoho OAuth."""
    organization = get_organization_from_request(request, org_id=org_id)
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
def vendors_sync_view(request, org_id):
    """Sync vendors from Zoho Books."""
    organization = get_organization_from_request(request, org_id=org_id)
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
def chart_of_accounts_sync_view(request, org_id):
    """Sync chart of accounts from Zoho Books."""
    organization = get_organization_from_request(request, org_id=org_id)
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
def taxes_sync_view(request, org_id):
    """Sync taxes from Zoho Books."""
    organization = get_organization_from_request(request, org_id=org_id)
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

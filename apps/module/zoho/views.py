# apps/module/zoho/views.py

import logging
import requests

from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.urls import reverse
from django.http import HttpResponseRedirect
from django.conf import settings

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from drf_spectacular.utils import extend_schema

import urllib.parse
import os

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
    """Get or create valid Zoho credentials for organization using environment variables."""
    # Get credentials from environment variables
    client_id = os.getenv('ZOHO_CLIENT_ID')
    client_secret = os.getenv('ZOHO_CLIENT_SECRET')
    redirect_url = os.getenv('ZOHO_REDIRECT_URL')
    
    if not all([client_id, client_secret, redirect_url]):
        raise ValueError("Zoho environment variables (ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REDIRECT_URL) are not configured")
    
    try:
        # Get existing credentials or create new ones
        credentials, created = ZohoCredentials.objects.get_or_create(
            organization=organization,
            defaults={
                'clientId': client_id,
                'clientSecret': client_secret,
                'redirectUrl': redirect_url,
                'organisationId': '',  # Will be set during OAuth flow
            }
        )
        
        # Update credentials with latest environment values if they exist
        if not created:
            credentials.clientId = client_id
            credentials.clientSecret = client_secret
            credentials.redirectUrl = redirect_url
            credentials.save(update_fields=['clientId', 'clientSecret', 'redirectUrl'])
        
        # Check token validity and refresh if needed
        if credentials.accessToken and not credentials.is_token_valid():
            if not credentials.refresh_token():
                # Clear invalid tokens to force re-authentication
                credentials.accessToken = None
                credentials.refreshToken = None
                credentials.save(update_fields=['accessToken', 'refreshToken'])
        
        return credentials
    except Exception as e:
        logger.error(f"Error managing Zoho credentials: {str(e)}")
        raise ValueError(f"Failed to get Zoho credentials: {str(e)}")


def make_zoho_api_request(credentials, endpoint, method='GET', data=None):
    """Make authenticated request to Zoho API."""
    headers = {
        'Authorization': f'Zoho-oauthtoken {credentials.accessToken}',
        'Content-Type': 'application/json'
    }

    # Handle endpoints that already have query parameters
    if '?' in endpoint:
        url = f"https://www.zohoapis.in/books/v3/{endpoint}&organization_id={credentials.organisationId}"
    else:
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
            return Response({}, status=status.HTTP_200_OK)
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
    responses={"200": {"authorization_url": "string", "state": "string"}},
    tags=["Zoho Ops"],
    methods=["POST"]
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def initiate_oauth_view(request, org_id):
    """Initiate Zoho OAuth2 flow using server-side credentials."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        # Get or create credentials using environment variables
        credentials = get_zoho_credentials(organization)
    except ValueError as e:
        return Response(
            {"detail": str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )

    # Generate state parameter for security (optional but recommended)
    import uuid
    state = str(uuid.uuid4())
    
    # Store state in session or database for validation (simple approach)
    request.session[f'zoho_oauth_state_{org_id}'] = state

    # Build Zoho OAuth2 authorization URL
    auth_params = {
        'response_type': 'code',
        'client_id': credentials.clientId,
        'scope': 'ZohoBooks.fullaccess.all',
        'redirect_uri': credentials.redirectUrl,
        'state': state,
        'access_type': 'offline'  # To get refresh token
    }
    
    authorization_url = 'https://accounts.zoho.in/oauth/v2/auth?' + urllib.parse.urlencode(auth_params)
    
    return Response({
        "authorization_url": authorization_url,
        "state": state,
        "detail": "Redirect user to authorization URL to complete OAuth flow"
    })


@extend_schema(
    responses={"200": {"access_token": "string", "refresh_token": "string", "expires_in": "integer"}},
    tags=["Zoho Ops"],
    methods=["GET"]
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def oauth_callback_view(request, org_id):
    """Handle OAuth2 callback from Zoho and exchange code for tokens."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    # Get authorization code and state from callback
    code = request.GET.get('code')
    state = request.GET.get('state')
    error = request.GET.get('error')
    
    if error:
        return Response({
            "detail": f"OAuth authorization failed: {error}",
            "error_description": request.GET.get('error_description', '')
        }, status=status.HTTP_400_BAD_REQUEST)
    
    if not code:
        return Response(
            {"detail": "Authorization code not received from Zoho"},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    # Validate state parameter
    expected_state = request.session.get(f'zoho_oauth_state_{org_id}')
    if state != expected_state:
        return Response(
            {"detail": "Invalid state parameter. Possible CSRF attack."},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    # Clean up state from session
    request.session.pop(f'zoho_oauth_state_{org_id}', None)

    try:
        # Get or create credentials using environment variables
        credentials = get_zoho_credentials(organization)
    except ValueError as e:
        return Response(
            {"detail": str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )

    # Exchange authorization code for tokens
    token_url = "https://accounts.zoho.in/oauth/v2/token"
    token_data = {
        'code': code,
        'client_id': credentials.clientId,
        'client_secret': credentials.clientSecret,
        'redirect_uri': credentials.redirectUrl,
        'grant_type': 'authorization_code'
    }

    try:
        response = requests.post(token_url, data=token_data, timeout=30)
        
        if response.status_code == 200:
            token_response = response.json()

            # Update credentials with new tokens
            credentials.accessToken = token_response.get('access_token')
            credentials.refreshToken = token_response.get('refresh_token')
            credentials.accessCode = code  # Store the code for reference

            # Set token expiry
            expires_in = token_response.get('expires_in', 3600)
            credentials.token_expiry = timezone.now() + timezone.timedelta(seconds=expires_in)

            # Get organization ID from Zoho after getting access token
            try:
                org_response = requests.get(
                    "https://www.zohoapis.in/books/v3/organizations",
                    headers={'Authorization': f'Zoho-oauthtoken {credentials.accessToken}'},
                    timeout=30
                )
                if org_response.status_code == 200:
                    org_data = org_response.json()
                    organizations = org_data.get('organizations', [])
                    if organizations:
                        # Use the first organization (in most cases there's only one)
                        credentials.organisationId = organizations[0]['organization_id']
                        logger.info(f"Set organization ID: {credentials.organisationId}")
            except Exception as org_error:
                logger.warning(f"Could not fetch organization ID: {str(org_error)}")
                # Continue without organization ID - can be set later

            credentials.save(update_fields=['accessToken', 'refreshToken', 'accessCode', 'token_expiry', 'organisationId', 'update_at'])

            return Response({
                "detail": "OAuth flow completed successfully",
                "success": True,
                "accessToken": credentials.accessToken[:20] + "...",  # Partial token for security
                "refreshToken": credentials.refreshToken[:20] + "...",
                "expires_in": expires_in,
                "token_expiry": credentials.token_expiry,
                "organization_id": credentials.organisationId
            })
        else:
            error_data = response.json() if response.content else {}
            logger.error(f"Zoho token exchange failed: {response.status_code} - {response.text}")
            
            return Response({
                "detail": f"Token exchange failed: {error_data.get('error_description', 'Unknown error')}",
                "error_code": error_data.get('error', 'token_exchange_failed')
            }, status=status.HTTP_400_BAD_REQUEST)
            
    except requests.RequestException as e:
        logger.error(f"Network error during token exchange: {str(e)}")
        return Response(
            {"detail": f"Network error: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
    except Exception as e:
        logger.error(f"Unexpected error during token exchange: {str(e)}")
        return Response(
            {"detail": f"Unexpected error: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


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
        # Get or create credentials using environment variables
        credentials = get_zoho_credentials(organization)
    except ValueError as e:
        return Response(
            {"detail": str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )

    if not credentials.accessCode or credentials.accessCode == "Your Access Code":
        return Response(
            {"detail": "Access code not provided. Please complete OAuth flow first."},
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


@extend_schema(
    responses={"200": {"is_connected": "boolean", "client_configured": "boolean", "organization_id": "string"}},
    tags=["Zoho Ops"],
    methods=["GET"]
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def zoho_status_view(request, org_id):
    """Get Zoho integration status for the organization."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    # Check if environment variables are configured
    client_configured = bool(
        os.getenv('ZOHO_CLIENT_ID') and 
        os.getenv('ZOHO_CLIENT_SECRET') and 
        os.getenv('ZOHO_REDIRECT_URL')
    )

    if not client_configured:
        return Response({
            "is_connected": False,
            "client_configured": False,
            "detail": "Zoho client credentials not configured on server"
        })

    try:
        credentials = get_zoho_credentials(organization)
        is_connected = bool(
            credentials.accessToken and 
            credentials.refreshToken and 
            credentials.is_token_valid()
        )
        
        return Response({
            "is_connected": is_connected,
            "client_configured": True,
            "organization_id": credentials.organisationId or None,
            "token_expiry": credentials.token_expiry,
            "created_at": credentials.created_at
        })
    except ValueError as e:
        return Response({
            "is_connected": False,
            "client_configured": True,
            "detail": str(e)
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


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
                        'gstNo': contact.get('gst_no', ''),
                        'gst_treatment': contact.get('gst_treatment', '')
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
        zoho_data = make_zoho_api_request(credentials, "settings/taxes")

        zoho_taxes = zoho_data.get("taxes", [])

        # Get existing tax IDs to avoid duplicates
        existing_taxes = ZohoTaxes.objects.filter(
            taxId__in=[tax["tax_id"] for tax in zoho_taxes],
            organization=organization
        ).values_list('taxId', flat=True)

        # Create new taxes
        new_taxes = []
        for tax in zoho_taxes:
            if tax["tax_id"] not in existing_taxes:
                new_taxes.append(ZohoTaxes(
                    taxId=tax["tax_id"],
                    taxName=tax["tax_name"],
                    organization=organization
                ))

        synced_count = 0
        if new_taxes:
            ZohoTaxes.objects.bulk_create(new_taxes)
            synced_count = len(new_taxes)

        return Response({
            "detail": f"Successfully synced {synced_count} taxes",
            "synced_count": synced_count
        })
    except Exception as e:
        logger.error(f"Taxes sync failed: {str(e)}")
        return Response(
            {"detail": f"Sync failed: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@extend_schema(
    responses=ZohoTdsTcsSerializer(many=True),
    tags=["Zoho Ops"],
    methods=["GET"],
    parameters=[
        {
            "name": "tax_type",
            "in": "query",
            "description": "Filter by tax type (TDS or TCS)",
            "required": False,
            "schema": {"type": "string", "enum": ["TDS", "TCS"]}
        }
    ]
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def tds_tcs_list_view(request, org_id):
    """List all TDS/TCS for the organization with pagination and optional filtering by tax type."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    # Start with base queryset
    tds_tcs = ZohoTdsTcs.objects.filter(organization=organization)

    # Apply tax_type filter if provided
    tax_type = request.query_params.get('tax_type')
    if tax_type and tax_type.upper() in ['TDS', 'TCS']:
        tds_tcs = tds_tcs.filter(taxType=tax_type.upper())

    # Order by taxName
    tds_tcs = tds_tcs.order_by('taxName')

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


@extend_schema(
    responses={"200": {"detail": "TDS/TCS synced successfully"}},
    tags=["Zoho Ops"],
    methods=["POST"]
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def tds_tcs_sync_view(request, org_id):
    """Sync TDS/TCS taxes from Zoho Books."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        credentials = get_zoho_credentials(organization)

        synced_count = 0

        # Fetch TDS taxes
        tds_data = make_zoho_api_request(credentials, "settings/taxes?is_tds_request=true")

        tds_taxes = tds_data.get('taxes', [])

        # Get existing TDS tax IDs to avoid duplicates
        existing_tds_taxes = ZohoTdsTcs.objects.filter(
            taxId__in=[tax["tax_id"] for tax in tds_taxes],
            taxType="TDS",
            organization=organization
        ).values_list('taxId', flat=True)

        # Create new TDS taxes
        new_tds_taxes = []
        for tax in tds_taxes:
            if tax["tax_id"] not in existing_tds_taxes:
                new_tds_taxes.append(ZohoTdsTcs(
                    taxId=tax["tax_id"],
                    taxName=tax["tax_name"],
                    taxPercentage=tax.get("tax_percentage", 0),
                    taxType="TDS",
                    organization=organization
                ))

        # Fetch TCS taxes
        tcs_data = make_zoho_api_request(credentials, "settings/taxes?is_tcs_request=true&filter_by=Taxes.All")
        tcs_taxes = tcs_data.get('taxes', [])

        # Get existing TCS tax IDs to avoid duplicates
        existing_tcs_taxes = ZohoTdsTcs.objects.filter(
            taxId__in=[tax["tax_id"] for tax in tcs_taxes],
            taxType="TCS",
            organization=organization
        ).values_list('taxId', flat=True)

        # Create new TCS taxes
        new_tcs_taxes = []
        for tax in tcs_taxes:
            if tax["tax_id"] not in existing_tcs_taxes:
                new_tcs_taxes.append(ZohoTdsTcs(
                    taxId=tax["tax_id"],
                    taxName=tax["tax_name"],
                    taxPercentage=tax.get("tax_percentage", 0),
                    taxType="TCS",
                    organization=organization
                ))

        # Bulk create new taxes
        if new_tds_taxes:
            ZohoTdsTcs.objects.bulk_create(new_tds_taxes)
            synced_count += len(new_tds_taxes)

        if new_tcs_taxes:
            ZohoTdsTcs.objects.bulk_create(new_tcs_taxes)
            synced_count += len(new_tcs_taxes)

        return Response({
            "detail": f"Successfully synced {synced_count} TDS/TCS taxes",
            "synced_count": synced_count,
            "tds_count": len(new_tds_taxes),
            "tcs_count": len(new_tcs_taxes)
        })

    except Exception as e:
        logger.error(f"TDS/TCS sync failed: {str(e)}")
        return Response(
            {"detail": f"Sync failed: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )

# apps/module/zoho/views/credentials.py
"""
Zoho credentials / OAuth management views.
"""
import logging
import os
import urllib.parse
import uuid

import requests
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..models import ZohoCredentials
from ..serializers.settings import ZohoCredentialsSerializer
from .helpers import get_organization_from_request, get_zoho_credentials

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Credentials CRUD
# ---------------------------------------------------------------------------


@extend_schema(responses=ZohoCredentialsSerializer, tags=["Zoho Ops"], methods=["GET"])
@extend_schema(request=ZohoCredentialsSerializer, responses=ZohoCredentialsSerializer, tags=["Zoho Ops"], methods=["PUT", "PATCH"])
@api_view(["GET", "PUT", "PATCH"])
@permission_classes([IsAuthenticated])
def zoho_credentials_view(request, org_id):
    """Get or update Zoho credentials for the organization."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        credentials = ZohoCredentials.objects.get(organization=organization)
    except ZohoCredentials.DoesNotExist:
        if request.method == "GET":
            return Response({"detail": "Zoho credentials not found"}, status=status.HTTP_200_OK)
        credentials = None

    if request.method == "GET":
        serializer = ZohoCredentialsSerializer(credentials)
        return Response(serializer.data)

    partial = request.method == "PATCH"
    if credentials:
        serializer = ZohoCredentialsSerializer(credentials, data=request.data, partial=partial)
    else:
        serializer = ZohoCredentialsSerializer(data=request.data)

    if serializer.is_valid():
        serializer.save(organization=organization)
        return Response(serializer.data)
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# ---------------------------------------------------------------------------
# OAuth flow
# ---------------------------------------------------------------------------


@extend_schema(
    responses={"200": {"authorization_url": "string", "state": "string"}},
    tags=["Zoho Ops"],
    methods=["POST"],
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def initiate_oauth_view(request, org_id):
    """Initiate Zoho OAuth2 flow using server-side credentials."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        credentials = get_zoho_credentials(organization)
    except ValueError as e:
        return Response({"detail": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    state = str(uuid.uuid4())
    request.session[f"zoho_oauth_state_{org_id}"] = state

    auth_params = {
        "response_type": "code",
        "client_id": credentials.clientId,
        "scope": (
            "ZohoBooks.contacts.ALL,ZohoBooks.settings.ALL,ZohoBooks.estimates.ALL,"
            "ZohoBooks.invoices.ALL,ZohoBooks.customerpayments.ALL,ZohoBooks.creditnotes.ALL,"
            "ZohoBooks.projects.ALL,ZohoBooks.expenses.ALL,ZohoBooks.salesorders.ALL,"
            "ZohoBooks.purchaseorders.ALL,ZohoBooks.bills.ALL,ZohoBooks.debitnotes.ALL,"
            "ZohoBooks.vendorpayments.ALL,ZohoBooks.banking.ALL,ZohoBooks.accountants.ALL"
        ),
        "redirect_uri": credentials.redirectUrl,
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    }

    authorization_url = "https://accounts.zoho.in/oauth/v2/auth?" + urllib.parse.urlencode(auth_params)

    return Response(
        {"authorization_url": authorization_url, "state": state, "detail": "Redirect user to authorization URL to complete OAuth flow"}
    )


@extend_schema(
    responses={"200": {"access_token": "string", "refresh_token": "string", "expires_in": "integer"}},
    tags=["Zoho Ops"],
    methods=["GET"],
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def oauth_callback_view(request, org_id):
    """Handle OAuth2 callback from Zoho and exchange code for tokens."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        logger.error(f"Organization not found for org_id: {org_id}")
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    code = request.GET.get("code")
    state = request.GET.get("state")
    error = request.GET.get("error")

    if error:
        logger.error(f"OAuth authorization failed: {error}")
        return Response(
            {"detail": f"OAuth authorization failed: {error}", "error_description": request.GET.get("error_description", "")},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if not code:
        logger.error("Authorization code not received from Zoho")
        return Response({"detail": "Authorization code not received from Zoho"}, status=status.HTTP_400_BAD_REQUEST)

    # State validation (non-blocking)
    expected_state = request.session.get(f"zoho_oauth_state_{org_id}")
    if expected_state and state != expected_state:
        logger.warning(f"State mismatch: expected {expected_state}, got {state}")
    if expected_state:
        request.session.pop(f"zoho_oauth_state_{org_id}", None)

    try:
        credentials = get_zoho_credentials(organization)
    except ValueError as e:
        logger.error(f"Failed to get credentials: {str(e)}")
        return Response({"detail": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    token_url = "https://accounts.zoho.in/oauth/v2/token"
    token_data = {
        "grant_type": "authorization_code",
        "client_id": credentials.clientId,
        "client_secret": credentials.clientSecret,
        "redirect_uri": credentials.redirectUrl,
        "code": code,
    }

    try:
        response = requests.post(token_url, data=token_data, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
        
        if response.status_code == 200:
            token_response = response.json()
            
            access_token = token_response.get("access_token")
            refresh_token = token_response.get("refresh_token")

            if not access_token:
                logger.error("Access token not received from Zoho")
                return Response(
                    {"detail": "Access token not received from Zoho", "error_code": "missing_access_token", "raw_response": token_response},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            credentials.accessToken = access_token
            credentials.refreshToken = refresh_token if refresh_token else None
            credentials.accessCode = code

            expires_in = token_response.get("expires_in", 3600)
            credentials.token_expiry = timezone.now() + timezone.timedelta(seconds=expires_in)

            # Fetch Zoho organization ID if not set
            org_id_set = False
            if not credentials.organisationId or credentials.organisationId == "Your organisationId":
                try:
                    org_response = requests.get(
                        "https://www.zohoapis.in/books/v3/organizations",
                        headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
                        timeout=30,
                    )
                    if org_response.status_code == 200:
                        organizations = org_response.json().get("organizations", [])
                        if organizations:
                            credentials.organisationId = organizations[0].get("organization_id", "")
                            org_id_set = True
                        else:
                            logger.warning("No organizations found in Zoho account")
                    else:
                        logger.warning(f"Failed to fetch organizations: {org_response.status_code}")
                except Exception as org_error:
                    logger.error(f"Could not fetch organization ID: {str(org_error)}")

            # Set connected status - require access token, refresh token, and valid orgId
            credentials.is_connected = bool(
                access_token 
                and refresh_token 
                and credentials.organisationId 
                and credentials.organisationId not in ["", "Your organisationId"]
            )
            
            credentials.save(update_fields=["accessToken", "refreshToken", "accessCode", "token_expiry", "organisationId", "is_connected", "update_at"])

            # Immediate refresh for a proper working access token
            immediate_refresh_success = False
            if refresh_token:
                immediate_refresh_success = credentials.refresh_token()
                if immediate_refresh_success:
                    access_token = credentials.accessToken
                    expires_in = 3600
                else:
                    logger.warning("Immediate token refresh failed")

            response_data = {
                "detail": "OAuth flow completed successfully",
                "success": True,
                "accessToken": access_token[:20] + "..." if len(access_token) > 20 else access_token,
                "refreshToken": (refresh_token[:20] + "..." if refresh_token and len(refresh_token) > 20 else ("Set" if refresh_token else "Not received")),
                "expires_in": expires_in,
                "token_expiry": credentials.token_expiry,
                "organization_id": credentials.organisationId,
                "org_id_fetched": org_id_set,
                "has_refresh_token": bool(credentials.refreshToken),
                "is_connected": credentials.is_connected,
                "api_domain": token_response.get("api_domain", "https://www.zohoapis.in"),
                "token_type": token_response.get("token_type", "Bearer"),
                "immediate_refresh_performed": bool(refresh_token),
                "immediate_refresh_success": immediate_refresh_success,
                "working_token_ready": immediate_refresh_success or not refresh_token,
            }
            return Response(response_data)
        else:
            error_data = {}
            try:
                if response.content:
                    error_data = response.json()
            except Exception:
                error_data = {"raw_response": response.text}

            logger.error(f"Token exchange failed - status: {response.status_code}, error: {error_data}")
            
            return Response(
                {
                    "detail": f"Token exchange failed: {error_data.get('error_description', 'Unknown error')}",
                    "success": False,
                    "error_code": error_data.get("error", "token_exchange_failed"),
                    "status_code": response.status_code,
                    "raw_error": error_data,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

    except requests.RequestException as e:
        logger.error(f"Network error during token exchange: {str(e)}", exc_info=True)
        return Response({"detail": f"Network error: {str(e)}", "success": False}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    except Exception as e:
        logger.error(f"Unexpected error during token exchange: {str(e)}", exc_info=True)
        return Response({"detail": f"Unexpected error: {str(e)}", "success": False}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@extend_schema(
    responses={"200": {"access_token": "string", "refresh_token": "string", "expires_in": "integer"}},
    tags=["Zoho Ops"],
    methods=["POST"],
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def generate_token_view(request, org_id):
    """Generate access and refresh tokens using the access code from Zoho OAuth."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        credentials = get_zoho_credentials(organization)
    except ValueError as e:
        return Response({"detail": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    if not credentials.accessCode or credentials.accessCode == "Your Access Code":
        return Response({"detail": "Access code not provided. Please complete OAuth flow first."}, status=status.HTTP_400_BAD_REQUEST)

    token_url = "https://accounts.zoho.in/oauth/v2/token"
    token_data = {
        "code": credentials.accessCode,
        "client_id": credentials.clientId,
        "client_secret": credentials.clientSecret,
        "redirect_uri": credentials.redirectUrl,
        "grant_type": "authorization_code",
    }

    try:
        response = requests.post(token_url, data=token_data, timeout=30)

        if response.status_code == 200:
            token_response = response.json()
            credentials.accessToken = token_response.get("access_token")
            credentials.refreshToken = token_response.get("refresh_token")
            expires_in = token_response.get("expires_in", 3600)
            credentials.token_expiry = timezone.now() + timezone.timedelta(seconds=expires_in)
            credentials.save(update_fields=["accessToken", "refreshToken", "token_expiry", "update_at"])

            return Response(
                {
                    "detail": "Tokens generated successfully",
                    "access_token": credentials.accessToken,
                    "refresh_token": credentials.refreshToken,
                    "expires_in": expires_in,
                    "token_expiry": credentials.token_expiry,
                }
            )
        else:
            error_data = response.json() if response.content else {}
            return Response(
                {
                    "detail": f"Token generation failed: {error_data.get('error_description', 'Unknown error')}",
                    "error_code": error_data.get("error", "token_generation_failed"),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

    except requests.RequestException as e:
        return Response({"detail": f"Network error: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    except Exception as e:
        return Response({"detail": f"Unexpected error: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@extend_schema(
    responses={"200": {"is_connected": "boolean", "client_configured": "boolean", "organization_id": "string"}},
    tags=["Zoho Ops"],
    methods=["GET"],
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def zoho_status_view(request, org_id):
    """Get Zoho integration status for the organization."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    client_configured = bool(os.getenv("ZOHO_CLIENT_ID") and os.getenv("ZOHO_CLIENT_SECRET") and os.getenv("ZOHO_REDIRECT_URL"))

    if not client_configured:
        return Response({"is_connected": False, "client_configured": False, "detail": "Zoho client credentials not configured on server"})

    try:
        credentials = get_zoho_credentials(organization)
        is_connected = bool(
            credentials.accessToken
            and credentials.refreshToken
            and credentials.is_token_valid()
            and hasattr(credentials, "is_connected")
            and credentials.is_connected
        )

        expected_connected = bool(credentials.accessToken and credentials.organisationId and credentials.is_token_valid())
        if hasattr(credentials, "is_connected") and credentials.is_connected != expected_connected:
            credentials.is_connected = expected_connected
            credentials.save(update_fields=["is_connected"])

        return Response(
            {
                "is_connected": is_connected,
                "client_configured": True,
                "organization_id": credentials.organisationId or None,
                "token_expiry": credentials.token_expiry,
                "created_at": credentials.created_at,
            }
        )
    except ValueError as e:
        return Response({"is_connected": False, "client_configured": True, "detail": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

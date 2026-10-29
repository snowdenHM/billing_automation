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

from apps.common import roles
from apps.common.permissions import HasOrgPermission, requires

from ..models import ZohoCredentials
from ..serializers.settings import ZohoCredentialsSerializer
from .helpers import get_organization_from_request, get_zoho_credentials

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Credentials CRUD
# ---------------------------------------------------------------------------


@extend_schema(responses=ZohoCredentialsSerializer, tags=["Zoho Ops"], methods=["GET"])
@extend_schema(request=ZohoCredentialsSerializer, responses=ZohoCredentialsSerializer, tags=["Zoho Ops"], methods=["PUT", "PATCH"])
@requires(roles.MANAGE_SETTINGS)
@api_view(["GET", "PUT", "PATCH"])
@permission_classes([IsAuthenticated, HasOrgPermission])
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
@requires(roles.MANAGE_SETTINGS)
@api_view(["POST"])
@permission_classes([IsAuthenticated, HasOrgPermission])
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
            "ZohoBooks.vendorpayments.ALL,ZohoBooks.banking.ALL,ZohoBooks.accountants.ALL,"
            # Lets us read back WHICH Zoho login authorised this
            # workspace. Without it, "this account has no Books
            # organisation" gives the operator no way to tell whether
            # they signed in with the wrong account — the single most
            # common cause — or genuinely have no organisation.
            "AaaServer.profile.READ"
        ),
        "redirect_uri": credentials.redirectUrl,
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    }

    authorization_url = (
        f"{credentials.accounts_base}/oauth/v2/auth?"
        + urllib.parse.urlencode(auth_params)
    )

    return Response(
        {"authorization_url": authorization_url, "state": state, "detail": "Redirect user to authorization URL to complete OAuth flow"}
    )


@extend_schema(
    responses={"200": {"access_token": "string", "refresh_token": "string", "expires_in": "integer"}},
    tags=["Zoho Ops"],
    methods=["GET"],
)
@requires(roles.MANAGE_SETTINGS)
@api_view(["GET"])
@permission_classes([IsAuthenticated, HasOrgPermission])
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

    token_url = f"{credentials.accounts_base}/oauth/v2/token"
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

            # Zoho names its own data centre in the token response. The
            # host used to be hardcoded to India, so an account on any
            # other DC authorised fine and then failed every call with
            # "this user is not associated with any organization" —
            # which reads like a missing organisation rather than a
            # wrong host.
            api_domain = (token_response.get("api_domain") or "").strip().rstrip("/")
            if api_domain:
                credentials.api_domain = api_domain

            # Fetch the Zoho organisation ID. Without it every later
            # call goes out as "?organization_id=" and Zoho rejects it.
            org_id_set = False
            org_problem = None
            org_chosen_note = None
            if not credentials.organisationId or credentials.organisationId == "Your organisationId":
                base = api_domain or getattr(credentials, "api_domain", "") or "https://www.zohoapis.in"
                try:
                    org_response = requests.get(
                        f"{base}/books/v3/organizations",
                        headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
                        timeout=30,
                    )
                    if org_response.status_code == 200:
                        organizations = org_response.json().get("organizations", [])
                        if organizations:
                            # Zoho's list order carries no meaning, so the
                            # account's default organisation is the only
                            # defensible pick. On a multi-organisation
                            # account the choice still has to be reported:
                            # a voucher posted into the wrong set of books
                            # looks like success until an accountant finds
                            # it there.
                            chosen = next(
                                (o for o in organizations if o.get("is_default_org")),
                                organizations[0],
                            )
                            credentials.organisationId = chosen.get("organization_id", "")
                            org_id_set = True
                            if len(organizations) > 1:
                                org_chosen_note = (
                                    f"This account can reach {len(organizations)} Zoho "
                                    f"Books organisations. Connected to "
                                    f"{chosen.get('name')} "
                                    f"({credentials.organisationId}) — its default. "
                                    f"If these books belong to another one, say so "
                                    f"before syncing."
                                )
                        else:
                            # Signing in to Zoho and having a Zoho BOOKS
                            # organisation are different things. This is
                            # the common case and it needs saying, not
                            # logging: the operator has to go and create
                            # one, or authorise as a user who belongs to
                            # the right one.
                            org_problem = (
                                "Authorisation succeeded, but this Zoho account has no "
                                "Zoho Books organisation. Create one in Zoho Books, or "
                                "reconnect using an account that belongs to the right "
                                "organisation."
                            )
                            logger.warning("No organizations found in Zoho account")
                    else:
                        try:
                            detail = org_response.json().get("message") or org_response.text[:200]
                        except Exception:
                            detail = org_response.text[:200]
                        org_problem = (
                            f"Could not read the Zoho organisation "
                            f"(HTTP {org_response.status_code}): {detail}"
                        )
                        logger.warning(f"Failed to fetch organizations: {org_response.status_code}")
                except Exception as org_error:
                    org_problem = (
                        "Could not reach Zoho to read the organisation ID. "
                        "Check the server's network connection and reconnect."
                    )
                    logger.error(f"Could not fetch organization ID: {str(org_error)}")

            # Set connected status - require access token, refresh token, and valid orgId
            credentials.is_connected = bool(
                access_token 
                and refresh_token 
                and credentials.organisationId 
                and credentials.organisationId not in ["", "Your organisationId"]
            )
            
            credentials.save(update_fields=["accessToken", "refreshToken", "accessCode", "token_expiry", "organisationId", "api_domain", "is_connected", "update_at"])

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
                # Tokens without an organisation id is a connection that
                # will fail on its first call. Say so here rather than
                # letting the operator discover it from five failing
                # syncs and a log they cannot read.
                "org_problem": org_problem,
                "org_chosen_note": org_chosen_note,
            }
            if org_problem:
                response_data["detail"] = org_problem
                response_data["success"] = False
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
@requires(roles.MANAGE_SETTINGS)
@api_view(["POST"])
@permission_classes([IsAuthenticated, HasOrgPermission])
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

    token_url = f"{credentials.accounts_base}/oauth/v2/token"
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
@requires(roles.VIEW_BILLS)
@api_view(["GET"])
@permission_classes([IsAuthenticated, HasOrgPermission])
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


def _authorised_as(credentials):
    """The Zoho login behind these tokens, or None.

    Needs the ``AaaServer.profile.READ`` scope, which older connections
    will not have — hence the quiet None rather than an error. Knowing
    the email turns "this account has no organisation" from a puzzle
    into something the operator can check in one glance.
    """
    import requests as _requests

    try:
        r = _requests.get(
            f"{credentials.accounts_base}/oauth/user/info",
            headers={"Authorization": f"Zoho-oauthtoken {credentials.accessToken}"},
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            return data.get("Email") or data.get("email") or None
    except Exception:
        pass
    return None


@extend_schema(
    summary="Test the Zoho Books connection",
    description=(
        "Calls Zoho with the stored credentials and reports what came "
        "back. Storing a token is not the same as having a working "
        "connection, and the settings page cannot tell the difference "
        "without asking."
    ),
    responses={200: None},
    tags=["Zoho Ops"],
)
@requires(roles.MANAGE_SETTINGS)
@api_view(["POST"])
@permission_classes([IsAuthenticated, HasOrgPermission])
def zoho_connection_test_view(request, org_id):
    """Ask Zoho whether these credentials actually work.

    The page used to show "Connected — Active" whenever tokens existed
    in the database. That is a claim about our own table, not about
    Zoho, and it stayed green while every sync returned 400. One cheap
    call settles it.
    """
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    credentials = ZohoCredentials.objects.filter(organization=organization).first()
    if credentials is None:
        return Response({
            "connected": False,
            "detail": "No Zoho credentials are saved for this workspace.",
        })

    missing = [
        label for label, value in (
            ("client ID", credentials.clientId),
            ("client secret", credentials.clientSecret),
            ("refresh token", credentials.refreshToken),
        ) if not value
    ]
    if missing:
        return Response({
            "connected": False,
            "detail": f"Incomplete credentials — missing {', '.join(missing)}.",
        })

    # A missing organisation ID is the one gap worth investigating
    # rather than reporting. The OAuth flow fills it from Zoho, so its
    # absence means Zoho had nothing to give — almost always because
    # the account has no Zoho Books organisation. Saying "missing
    # organisation ID" sends the operator looking for a field to type
    # into; asking Zoho sends them to the thing they actually have to
    # create.
    if not credentials.organisationId:
        from .helpers import ZohoApiError, zoho_api_base
        import requests as _requests

        try:
            probe = _requests.get(
                f"{zoho_api_base(credentials)}/books/v3/organizations",
                headers={"Authorization": f"Zoho-oauthtoken {credentials.accessToken}"},
                timeout=20,
            )
        except Exception:
            return Response({
                "connected": False,
                "detail": "No organisation ID is stored, and Zoho could not be reached to read one.",
            })

        if probe.status_code == 200:
            orgs = probe.json().get("organizations") or []
            if not orgs:
                who = _authorised_as(credentials)
                whose = f" ({who})" if who else ""
                return Response({
                    "connected": False,
                    "authorised_as": who,
                    "detail": (
                        f"The Zoho account this workspace is connected to{whose} "
                        f"has no Zoho Books organisation. Signing in to Zoho and "
                        f"having a Books organisation are different things. Either "
                        f"create one in Zoho Books, or reconnect using the account "
                        f"that owns your books."
                    ),
                })
            # One organisation and nothing to choose between: adopt it.
            # Zoho just named it, so sending the operator back through
            # OAuth to copy a value we are already holding is friction
            # for its own sake. This is also the state a workspace lands
            # in when the Books organisation was created *after* the
            # OAuth round — the common case, since the connect flow is
            # what tells people they need one.
            if len(orgs) == 1:
                credentials.organisationId = str(orgs[0].get("organization_id") or "")
                credentials.is_connected = bool(credentials.organisationId)
                credentials.save(update_fields=[
                    "organisationId", "is_connected", "update_at",
                ])
                return Response({
                    "connected": True,
                    "detail": (
                        f"Connected to {orgs[0].get('name')}. Its organisation "
                        f"ID was read from Zoho and saved — run the syncs next."
                    ),
                    "organization_name": orgs[0].get("name"),
                    "organization_id": credentials.organisationId,
                    "currency": orgs[0].get("currency_code"),
                    "adopted": True,
                })

            # Several organisations: do not guess. Picking the wrong one
            # posts vouchers into the wrong set of books, and nothing
            # about the mistake looks like an error until an accountant
            # finds them there.
            names = ", ".join(
                f"{o.get('name')} ({o.get('organization_id')})" for o in orgs[:5]
            )
            return Response({
                "connected": False,
                "detail": (
                    f"No organisation ID is stored, and this account can reach "
                    f"more than one: {names}. Reconnect and pick the one these "
                    f"books belong to."
                ),
            })

        try:
            why = probe.json().get("message") or probe.text[:200]
        except Exception:
            why = probe.text[:200]
        return Response({
            "connected": False,
            "detail": f"No organisation ID is stored. Zoho said: {why}",
        })

    # `organizations` is the cheapest authenticated call Zoho offers and
    # it needs no extra scope beyond what any sync already uses.
    from .helpers import ZohoApiError, make_zoho_api_request

    try:
        payload = make_zoho_api_request(credentials, "organizations")
    except ZohoApiError as exc:
        return Response({
            "connected": False,
            "detail": exc.message,
            "zoho_code": exc.zoho_code,
        })
    except Exception:
        logger.exception("Zoho connection test failed for org %s", organization.id)
        return Response({
            "connected": False,
            "detail": "The connection test failed. Check the server logs.",
        })

    orgs = payload.get("organizations") or []
    match = next(
        (o for o in orgs if str(o.get("organization_id")) == str(credentials.organisationId)),
        None,
    )
    if match is None:
        names = ", ".join(
            f"{o.get('name')} ({o.get('organization_id')})" for o in orgs[:5]
        ) or "none"
        return Response({
            "connected": False,
            "detail": (
                f"Zoho answered, but organisation ID "
                f"{credentials.organisationId} is not among the ones these "
                f"credentials can reach. Available: {names}."
            ),
        })

    return Response({
        "connected": True,
        "detail": f"Connected to {match.get('name')}.",
        "organization_name": match.get("name"),
        "organization_id": match.get("organization_id"),
        "currency": match.get("currency_code"),
    })

# apps/users/views/auth.py
"""
Authentication views: register, login, password reset, email verification, token refresh.
"""
import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.common.authentication import OptionalJWTAuthentication
from apps.common.utils import (
    get_rate_limit_ip,
    is_rate_limited,
    send_simple_email,
    send_templated_email,
)
from apps.users.tokens import email_verification_token

logger = logging.getLogger(__name__)


def _display_name(user):
    return (
        user.get_full_name() if hasattr(user, "get_full_name") and user.get_full_name()
        else getattr(user, "first_name", "") or user.email.split("@")[0]
    )


def _build_frontend_url(path):
    base = getattr(settings, "FRONTEND_URL", "https://billmunshi.com").rstrip("/")
    return f"{base}{path}"


def _send_verify_email(user):
    """Generate a signed verification link and send the branded email.

    Returns ``(verify_url, sent)``. ``sent`` is False when the provider
    refused the message.

    Callers must not assume delivery. This swallowed its exception and
    returned only the URL, so every caller carried on as if the mail had
    gone out — which is how a signup could answer "we've sent a
    verification link to <email>" while SendGrid was returning
    "Maximum credits exceeded" and nothing had been sent at all. The user
    then waits for a mail that will never arrive, and because
    ``email_verification_required`` blocks login, the account is unusable
    with no visible reason.
    """
    token = email_verification_token.make_token(user)
    uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
    verify_url = _build_frontend_url(f"/auth/verify-email?uidb64={uidb64}&token={token}")
    try:
        send_templated_email(
            subject="Confirm your Bill Munshi email",
            template_base="verify_email",
            to_email=user.email,
            context={
                "user": user,
                "display_name": _display_name(user),
                "verify_url": verify_url,
            },
        )
    except Exception as exc:
        logger.error("Verify-email send failed for %s: %s", user.email, exc)
        return verify_url, False
    return verify_url, True
from apps.users.serializers import (
    RegisterSerializer,
    LoginSerializer,
    PasswordResetRequestSerializer,
    PasswordResetConfirmSerializer,
    ChangePasswordSerializer,
    UserSerializer,
    RefreshTokenSerializer,
)

User = get_user_model()


@extend_schema(request=RegisterSerializer, responses=UserSerializer, tags=["Auth"], methods=["POST"])
@api_view(["POST"])
@permission_classes([AllowAny])
def register_view(request):
    """Create a new user account."""
    serializer = RegisterSerializer(data=request.data, context={"request": request})
    serializer.is_valid(raise_exception=True)
    user = serializer.save()

    # Fire the welcome + verify-email flow. Failures are logged, never
    # fatal — signup itself succeeds even if SMTP is temporarily down.
    verify_url, verification_sent = _send_verify_email(user)
    try:
        send_templated_email(
            subject="Welcome to Bill Munshi",
            template_base="welcome",
            to_email=user.email,
            context={
                "user": user,
                "display_name": _display_name(user),
                "verify_url": verify_url,
            },
        )
    except Exception as exc:
        logger.error("Welcome email send failed for %s: %s", user.email, exc)

    # Say what actually happened. The account exists either way — the
    # signup itself succeeded — but claiming a link was sent when the
    # provider rejected it leaves the user watching an inbox for nothing,
    # unable to log in and with no idea why.
    if verification_sent:
        message = (
            "Account created. We've sent a verification link to "
            f"{user.email} — please verify your email to log in."
        )
    else:
        message = (
            f"Account created for {user.email}, but we couldn't send the "
            "verification email just now. Use \u201cResend verification "
            "email\u201d below in a few minutes, or contact support if it "
            "keeps failing."
        )

    return Response(
        {
            "user": UserSerializer(user, context={"request": request}).data,
            "email_verification_required": True,
            "verification_email_sent": verification_sent,
            "message": message,
        },
        status=status.HTTP_201_CREATED,
    )


@extend_schema(request=LoginSerializer, responses=LoginSerializer, tags=["Auth"], methods=["POST"])
@api_view(["POST"])
@permission_classes([AllowAny])
def login_view(request):
    """Authenticate a user with email and password, returning JWT tokens."""
    email = ((request.data or {}).get("email") or "").strip().lower()
    ip = get_rate_limit_ip(request)
    if (
        is_rate_limited("login-ip", ip, limit=30, window_seconds=900)
        or is_rate_limited("login-email", email, limit=10, window_seconds=900)
    ):
        return Response(
            {"detail": "Too many login attempts. Please wait a few minutes and try again."},
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    serializer = LoginSerializer(data=request.data, context={"request": request})
    serializer.is_valid(raise_exception=True)
    return _attach_refresh_cookie(
        Response(serializer.validated_data, status=status.HTTP_200_OK),
        serializer.validated_data,
        keep_in_body=_asks_for_refresh_in_body(request),
    )


# ---------------------------------------------------------------------------
# Refresh token transport
#
# The refresh token used to be returned in the JSON body, and the SPA put
# it in localStorage. Any script running on the origin could read it, and
# because a refresh token mints new access tokens, one XSS became durable
# account takeover rather than a session that expires.
#
# It now travels in an httpOnly cookie, which JavaScript cannot read. The
# body still carries it during the migration so a client running the old
# bundle keeps working; once every session has rolled over,
# ``_REFRESH_IN_BODY`` can be flipped off and the token stops being
# readable anywhere.
# ---------------------------------------------------------------------------

REFRESH_COOKIE_NAME = "bm_refresh"

#: Keep returning the refresh token in the JSON body.
#:
#: Off as of 2026-11-14. The shipped bundle has been cookie-only since
#: the 2026-11-11 deploy: it never reads ``data.refresh``, and it clears
#: any leftover ``refresh_token`` from localStorage on its first
#: refresh. With this off the token is no longer readable by script
#: anywhere, which was the whole point of moving it to an httpOnly
#: cookie.
#:
#: The cost is one reload for anyone still running the pre-deploy bundle
#: in a tab they have not refreshed since. Set back to True if that
#: turns out to be anybody.
_REFRESH_IN_BODY = False

#: The mobile app has no cookie jar it can rely on and keeps the token in
#: the OS keystore instead, which page scripts cannot reach. It sends
#: ``X-Token-Transport: body`` on login to get the token in the JSON.
#:
#: Only login honours it, because login needs the password. On refresh a
#: script injected into the site could send the header too, the browser
#: would attach the cookie, and the script would get the very token this
#: section keeps from it.
_TOKEN_TRANSPORT_HEADER = "HTTP_X_TOKEN_TRANSPORT"


def _asks_for_refresh_in_body(request):
    return request.META.get(_TOKEN_TRANSPORT_HEADER, "").strip().lower() == "body"


def _attach_refresh_cookie(response, data, *, keep_in_body=False):
    """Move the refresh token from *data* into an httpOnly cookie.

    With *keep_in_body* the cookie is still set but the token stays in
    the JSON as well, for a client that stores it itself.
    """
    refresh = (data or {}).get("refresh")
    if not refresh:
        return response

    secure = not settings.DEBUG
    response.set_cookie(
        REFRESH_COOKIE_NAME,
        refresh,
        max_age=int(
            settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"].total_seconds()
        ) if "REFRESH_TOKEN_LIFETIME" in getattr(settings, "SIMPLE_JWT", {}) else 7 * 24 * 3600,
        httponly=True,
        secure=secure,
        samesite="Lax",
        # Scoped to the refresh endpoint so it is not attached to every
        # API call, which keeps it out of logs and proxies.
        path="/api/v1/auth/refresh/",
    )
    if not (_REFRESH_IN_BODY or keep_in_body):
        response.data = {k: v for k, v in response.data.items() if k != "refresh"}
    return response


def _clear_refresh_cookie(response):
    response.delete_cookie(REFRESH_COOKIE_NAME, path="/api/v1/auth/refresh/")
    return response


@extend_schema(request=None, responses={"200": None}, tags=["Auth"], methods=["POST"])
@api_view(["POST"])
@permission_classes([AllowAny])
def logout_view(request):
    """End the session: blacklist the refresh token and drop its cookie.

    Needed because the refresh token now lives in an httpOnly cookie.
    The browser cannot delete it — only the server that set it can — so
    without this, signing out would clear the visible tokens and leave a
    working refresh token in the browser. That is worse than where we
    started.

    AllowAny because a caller whose access token has already expired must
    still be able to sign out; the refresh cookie is what is being
    revoked, and presenting it is the only authorisation this needs.
    """
    token = request.COOKIES.get(REFRESH_COOKIE_NAME) or (request.data or {}).get("refresh")

    if token:
        try:
            from rest_framework_simplejwt.tokens import RefreshToken
            RefreshToken(token).blacklist()
        except Exception as exc:
            # An already-expired, already-blacklisted or malformed token
            # is not a failure to sign out — the cookie still goes.
            logger.info("Logout: refresh token not blacklisted (%s)", exc)

    return _clear_refresh_cookie(
        Response({"detail": "Signed out."}, status=status.HTTP_200_OK)
    )

@extend_schema(request=PasswordResetRequestSerializer, responses={"200": None}, tags=["Auth"], methods=["POST"])
@api_view(["POST"])
@permission_classes([AllowAny])
def password_reset_request_view(request):
    """Request a password reset. Sends a reset email if user exists."""
    serializer = PasswordResetRequestSerializer(data=request.data, context={"request": request})
    serializer.is_valid(raise_exception=True)
    email = serializer.validated_data["email"]

    ip = get_rate_limit_ip(request)
    if (
        is_rate_limited("pwreset-ip", ip, limit=20, window_seconds=3600)
        or is_rate_limited("pwreset-email", email, limit=3, window_seconds=900)
    ):
        # Generic body, matching the success reply, so the throttle itself
        # doesn't become an account-enumeration oracle.
        return Response(
            {"detail": "If the email exists, a reset link was sent."},
            status=status.HTTP_200_OK,
        )

    user = User.objects.filter(email__iexact=email, is_active=True).first()

    if user:
        token = PasswordResetTokenGenerator().make_token(user)
        uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
        reset_url = _build_frontend_url(
            f"/auth/reset-password?uidb64={uidb64}&token={token}"
        )
        # The link's lifetime is PASSWORD_RESET_TIMEOUT, set in
        # settings (two hours, down from Django's three-day default).
        # Shown to the reader in minutes so the email can say it.
        ttl_minutes = int(getattr(settings, "PASSWORD_RESET_TIMEOUT", 3 * 24 * 3600) / 60)
        try:
            send_templated_email(
                subject="Reset your Bill Munshi password",
                template_base="password_reset",
                to_email=user.email,
                context={
                    "user": user,
                    "display_name": _display_name(user),
                    "reset_url": reset_url,
                    "ttl_minutes": ttl_minutes,
                },
            )
        except Exception as exc:
            logger.error("Password-reset email send failed for %s: %s", user.email, exc)

    # Always respond generically so the endpoint doesn't leak which
    # emails exist in the DB.
    return Response({"detail": "If the email exists, a reset link was sent."})




def _revoke_all_refresh_tokens(user):
    """Blacklist every outstanding refresh token this user holds.

    A password change has to end the other sessions, or it does not do
    the one job people change their password for. Rotation plus
    blacklist-after-rotation only retires a token once it is *used*; a
    stolen refresh token that is simply held stays valid for its whole
    seven days, so the attacker keeps the account for a week after the
    victim "locked them out".

    Never raises: a password that was changed but whose sessions could
    not be cleared is still a password that was changed, and failing the
    request would leave the user worse off.
    """
    try:
        from rest_framework_simplejwt.token_blacklist.models import (
            BlacklistedToken, OutstandingToken,
        )

        revoked = 0
        for token in OutstandingToken.objects.filter(user=user):
            _, created = BlacklistedToken.objects.get_or_create(token=token)
            revoked += 1 if created else 0
        if revoked:
            logger.info("Revoked %d refresh token(s) for user %s", revoked, user.pk)
        return revoked
    except Exception as exc:
        logger.warning("Could not revoke refresh tokens for user %s: %s", user.pk, exc)
        return 0


@extend_schema(request=PasswordResetConfirmSerializer, responses={"200": None}, tags=["Auth"], methods=["POST"])
@api_view(["POST"])
@permission_classes([AllowAny])
def password_reset_confirm_view(request):
    """Validate the reset token and set a new password."""
    serializer = PasswordResetConfirmSerializer(data=request.data, context={"request": request})
    serializer.is_valid(raise_exception=True)
    user = serializer.validated_data["user_obj"]
    user.set_password(serializer.validated_data["new_password"])
    user.save(update_fields=["password"])
    # Whoever had this account before this reset does not keep it.
    _revoke_all_refresh_tokens(user)
    return _clear_refresh_cookie(
        Response({"detail": "Password has been reset successfully."})
    )


@extend_schema(request=ChangePasswordSerializer, responses={"200": None}, tags=["Auth"], methods=["POST"])
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def change_password_view(request):
    """Authenticated password change (requires current password)."""
    serializer = ChangePasswordSerializer(data=request.data, context={"request": request})
    serializer.is_valid(raise_exception=True)
    request.user.set_password(serializer.validated_data["new_password"])
    request.user.save(update_fields=["password"])
    # Every other session this account had ends here. The caller's own
    # access token keeps working until it expires, which is what makes
    # the response usable; their refresh token does not.
    _revoke_all_refresh_tokens(request.user)
    return _clear_refresh_cookie(
        Response({"detail": "Password changed successfully."})
    )


@extend_schema(responses={"200": None}, tags=["Auth"], methods=["GET"])
@api_view(["GET"])
@authentication_classes([OptionalJWTAuthentication])
@permission_classes([AllowAny])
def verify_email_view(request, uidb64, token):
    """Verify a user's email address from the link in the verification email."""
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.get(pk=uid)
    except Exception:
        return Response({"detail": "Invalid verification link"}, status=status.HTTP_400_BAD_REQUEST)

    if user.email_verified:
        return Response({"detail": "Email already verified. You can log in."})

    if not email_verification_token.check_token(user, token):
        return Response({"detail": "Invalid or expired verification token"}, status=status.HTTP_400_BAD_REQUEST)

    user.email_verified = True
    user.save(update_fields=["email_verified"])
    return Response({"detail": "Email verified successfully."})


@extend_schema(responses={"200": None}, tags=["Auth"], methods=["POST"])
@api_view(["POST"])
@authentication_classes([OptionalJWTAuthentication])
@permission_classes([AllowAny])
def resend_verification_email_view(request):
    """Re-send the email-verification link (Correction 57).

    Always answers with the same generic message so it can't be used to
    probe which emails are registered.
    """
    email = ((request.data or {}).get("email") or "").strip().lower()
    generic = {
        "detail": "If this email needs verification, a new link has been sent."
    }
    if not email:
        return Response({"email": ["This field is required."]}, status=status.HTTP_400_BAD_REQUEST)

    ip = get_rate_limit_ip(request)
    if (
        is_rate_limited("verify-resend-ip", ip, limit=20, window_seconds=3600)
        or is_rate_limited("verify-resend-email", email, limit=3, window_seconds=900)
    ):
        return Response(
            {"detail": "Too many requests. Please wait a few minutes and try again."},
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    user = User.objects.filter(email__iexact=email, is_active=True).first()
    if user and not user.email_verified:
        _, sent = _send_verify_email(user)
        if not sent:
            # Trade-off, taken deliberately. The generic reply above exists
            # so this endpoint cannot be used to probe which addresses are
            # registered, and a 503 here is only reachable when an account
            # was in fact found — so during a provider outage it does leak
            # that much.
            #
            # Returning the usual "a new link has been sent" would be worse:
            # the user retries, is told it worked every time, and never
            # learns the mail is not coming. The leak needs our mail
            # provider to be down to exist at all; the lie is permanent.
            return Response(
                {
                    "detail": (
                        "Our email service is temporarily unavailable, so the "
                        "link could not be sent. Please try again in a few "
                        "minutes."
                    )
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
    return Response(generic)


@extend_schema(request=RefreshTokenSerializer, responses=RefreshTokenSerializer, tags=["Auth"], methods=["POST"])
@api_view(["POST"])
@permission_classes([AllowAny])
def refresh_token_view(request):
    """Refresh JWT access tokens using a valid refresh token.

    The token is read from the httpOnly cookie first and from the body
    only as a fallback, so a browser never has to hold it in storage
    while a client on the previous bundle still works.
    """
    data = dict(request.data or {})
    cookie_token = request.COOKIES.get(REFRESH_COOKIE_NAME)
    if cookie_token and not data.get("refresh"):
        data["refresh"] = cookie_token

    serializer = RefreshTokenSerializer(data=data, context={"request": request})
    serializer.is_valid(raise_exception=True)
    # A rotated refresh token has to replace the cookie, or the next
    # refresh presents a token the server has already retired.
    return _attach_refresh_cookie(
        Response(serializer.validated_data, status=status.HTTP_200_OK),
        serializer.validated_data,
    )

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
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.common.utils import send_simple_email, send_templated_email

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
    """Generate a signed verification link and send the branded email."""
    token = PasswordResetTokenGenerator().make_token(user)
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
    return verify_url
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
    verify_url = _send_verify_email(user)
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

    return Response(
        {"user": UserSerializer(user, context={"request": request}).data},
        status=status.HTTP_201_CREATED,
    )


@extend_schema(request=LoginSerializer, responses=LoginSerializer, tags=["Auth"], methods=["POST"])
@api_view(["POST"])
@permission_classes([AllowAny])
def login_view(request):
    """Authenticate a user with email and password, returning JWT tokens."""
    serializer = LoginSerializer(data=request.data, context={"request": request})
    serializer.is_valid(raise_exception=True)
    return Response(serializer.validated_data, status=status.HTTP_200_OK)


@extend_schema(request=PasswordResetRequestSerializer, responses={"200": None}, tags=["Auth"], methods=["POST"])
@api_view(["POST"])
@permission_classes([AllowAny])
def password_reset_request_view(request):
    """Request a password reset. Sends a reset email if user exists."""
    serializer = PasswordResetRequestSerializer(data=request.data, context={"request": request})
    serializer.is_valid(raise_exception=True)
    email = serializer.validated_data["email"]
    user = User.objects.filter(email__iexact=email, is_active=True).first()

    if user:
        token = PasswordResetTokenGenerator().make_token(user)
        uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
        reset_url = _build_frontend_url(
            f"/auth/reset-password?uidb64={uidb64}&token={token}"
        )
        # Django's default PasswordResetTokenGenerator TTL is
        # PASSWORD_RESET_TIMEOUT (3 days). Show minutes in a human way.
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
    return Response({"detail": "Password has been reset successfully."})


@extend_schema(request=ChangePasswordSerializer, responses={"200": None}, tags=["Auth"], methods=["POST"])
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def change_password_view(request):
    """Authenticated password change (requires current password)."""
    serializer = ChangePasswordSerializer(data=request.data, context={"request": request})
    serializer.is_valid(raise_exception=True)
    request.user.set_password(serializer.validated_data["new_password"])
    request.user.save(update_fields=["password"])
    return Response({"detail": "Password changed successfully."})


@extend_schema(responses={"200": None}, tags=["Auth"], methods=["GET"])
@api_view(["GET"])
@permission_classes([AllowAny])
def verify_email_view(request, uidb64, token):
    """Verify a user's email address from the link in the verification email."""
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.get(pk=uid)
    except Exception:
        return Response({"detail": "Invalid verification link"}, status=status.HTTP_400_BAD_REQUEST)

    if not PasswordResetTokenGenerator().check_token(user, token):
        return Response({"detail": "Invalid or expired verification token"}, status=status.HTTP_400_BAD_REQUEST)

    user.email_verified = True
    user.save(update_fields=["email_verified"])
    return Response({"detail": "Email verified successfully."})


@extend_schema(request=RefreshTokenSerializer, responses=RefreshTokenSerializer, tags=["Auth"], methods=["POST"])
@api_view(["POST"])
@permission_classes([AllowAny])
def refresh_token_view(request):
    """Refresh JWT access tokens using a valid refresh token."""
    serializer = RefreshTokenSerializer(data=request.data, context={"request": request})
    serializer.is_valid(raise_exception=True)
    return Response(serializer.validated_data, status=status.HTTP_200_OK)

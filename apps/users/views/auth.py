# apps/users/views/auth.py
"""
Authentication views: register, login, password reset, email verification, token refresh.
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.common.utils import send_simple_email
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
        reset_url = f"https://billmunshi.com/reset-password?uidb64={uidb64}&token={token}"
        message = (
            f"Hello {user.get_full_name() or user.email},\n\n"
            f"Please click the link below to reset your password:\n"
            f"{reset_url}\n\n"
            f"If you didn't request this password reset, please ignore this email.\n\n"
            f"Best regards,\nThe Bill Munshi Team"
        )
        send_simple_email("Password Reset", message, to_email=user.email)

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

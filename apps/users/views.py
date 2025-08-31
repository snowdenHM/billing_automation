# apps/users/views.py

from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.utils.encoding import force_bytes
from django.db.models import Prefetch
from django.utils.http import urlsafe_base64_encode
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.generics import RetrieveUpdateAPIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.organizations.models import OrgMembership
from apps.common.utils import send_simple_email
from .serializers import (
    RegisterSerializer,
    LoginSerializer,
    PasswordResetRequestSerializer,
    PasswordResetConfirmSerializer,
    ChangePasswordSerializer,
    UserSerializer,
)

User = get_user_model()


class RegisterView(APIView):
    """
    API endpoint for user registration.

    Creates a new user account with the provided email, password, and profile information.
    """
    permission_classes = [AllowAny]
    serializer_class = RegisterSerializer

    @extend_schema(request=RegisterSerializer, responses=UserSerializer, tags=["Auth"])
    def post(self, request, *args, **kwargs):
        serializer = RegisterSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        user = serializer.save()

        # Return user data with a 201 Created status
        return Response(
            {"user": UserSerializer(user, context={"request": request}).data},
            status=status.HTTP_201_CREATED,
        )


class LoginView(APIView):
    """
    API endpoint for user authentication.

    Authenticates a user with email and password, returning JWT tokens and user data.
    """
    permission_classes = [AllowAny]
    serializer_class = LoginSerializer

    @extend_schema(request=LoginSerializer, responses=LoginSerializer, tags=["Auth"])
    def post(self, request, *args, **kwargs):
        serializer = LoginSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        return Response(serializer.validated_data, status=status.HTTP_200_OK)


class PasswordResetRequestView(APIView):
    """
    API endpoint to request a password reset.

    Sends a password reset email to the provided email address if a user exists.
    """
    permission_classes = [AllowAny]
    serializer_class = PasswordResetRequestSerializer

    @extend_schema(request=PasswordResetRequestSerializer, responses={"200": None}, tags=["Auth"])
    def post(self, request, *args, **kwargs):
        serializer = PasswordResetRequestSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]
        user = User.objects.filter(email__iexact=email, is_active=True).first()

        if user:
            token = PasswordResetTokenGenerator().make_token(user)
            uidb64 = urlsafe_base64_encode(force_bytes(user.pk))

            # For production, you'd create a proper password reset URL
            reset_url = f"https://billmunshi.com/reset-password?uidb64={uidb64}&token={token}"

            # Send email with reset link
            message = (
                f"Hello {user.get_full_name() or user.email},\n\n"
                f"Please click the link below to reset your password:\n"
                f"{reset_url}\n\n"
                f"If you didn't request this password reset, please ignore this email.\n\n"
                f"Best regards,\nThe Bill Munshi Team"
            )
            send_simple_email("Password Reset", message, to_email=user.email)

        # Always return a success response, even if no user is found
        # This prevents email enumeration attacks
        return Response({"detail": "If the email exists, a reset link was sent."})


class PasswordResetConfirmView(APIView):
    """
    API endpoint to confirm a password reset.

    Validates the reset token and sets a new password for the user.
    """
    permission_classes = [AllowAny]
    serializer_class = PasswordResetConfirmSerializer

    @extend_schema(request=PasswordResetConfirmSerializer, responses={"200": None}, tags=["Auth"])
    def post(self, request, *args, **kwargs):
        serializer = PasswordResetConfirmSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        user = serializer.validated_data["user_obj"]
        new_password = serializer.validated_data["new_password"]

        # Set new password and save
        user.set_password(new_password)
        user.save(update_fields=["password"])

        return Response({"detail": "Password has been reset successfully."})


class ChangePasswordView(APIView):
    """
    API endpoint for authenticated users to change their password.

    Requires the current password and a new password.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = ChangePasswordSerializer

    @extend_schema(request=ChangePasswordSerializer, responses={"200": None}, tags=["Auth"])
    def post(self, request, *args, **kwargs):
        serializer = ChangePasswordSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        # Update the user's password
        user = request.user
        user.set_password(serializer.validated_data["new_password"])
        user.save(update_fields=["password"])

        return Response({"detail": "Password changed successfully."})


class MeView(RetrieveUpdateAPIView):
    """
    API endpoint for retrieving and updating the authenticated user's profile.

    Provides GET, PUT, and PATCH methods for user profile management.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = UserSerializer

    def get_object(self):
        """Get current user with prefetched org memberships."""
        user = self.request.user
        user.update_last_active()

        # Prefetch active organization memberships to optimize the serializer
        return User.objects.filter(id=user.id).prefetch_related(
            Prefetch(
                "memberships",  # Using correct related_name from OrgMembership model
                queryset=OrgMembership.objects.filter(is_active=True).select_related("organization"),
                to_attr="active_memberships",
            )
        ).first()

    @extend_schema(responses=UserSerializer, tags=["Auth"])
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(request=UserSerializer, responses=UserSerializer, tags=["Auth"])
    def put(self, request, *args, **kwargs):
        return super().put(request, *args, **kwargs)

    @extend_schema(request=UserSerializer, responses=UserSerializer, tags=["Auth"])
    def patch(self, request, *args, **kwargs):
        return super().patch(request, *args, **kwargs)


class VerifyEmailView(APIView):
    """
    API endpoint to verify a user's email address.

    This would typically be called from a link in a verification email.
    """
    permission_classes = [AllowAny]

    @extend_schema(responses={"200": None}, tags=["Auth"])
    def get(self, request, uidb64, token):
        try:
            uid = urlsafe_base64_decode(uidb64).decode()
            user = User.objects.get(pk=uid)
        except Exception:
            return Response({"detail": "Invalid verification link"}, status=status.HTTP_400_BAD_REQUEST)

        # Use a token generator similar to password reset
        if not PasswordResetTokenGenerator().check_token(user, token):
            return Response({"detail": "Invalid or expired verification token"}, status=status.HTTP_400_BAD_REQUEST)

        # Mark email as verified
        user.email_verified = True
        user.save(update_fields=["email_verified"])

        return Response({"detail": "Email verified successfully."})

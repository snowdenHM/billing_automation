# apps/users/views.py

from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.db.models import Prefetch
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.organizations.models import OrgMembership
from apps.common.utils import send_simple_email
from apps.common.permissions import IsSuperAdmin
from .serializers import (
    RegisterSerializer,
    LoginSerializer,
    PasswordResetRequestSerializer,
    PasswordResetConfirmSerializer,
    ChangePasswordSerializer,
    UserSerializer,
    RefreshTokenSerializer,
)

User = get_user_model()


@extend_schema(
    request=RegisterSerializer,
    responses=UserSerializer,
    tags=["Auth"],
    methods=["POST"]
)
@api_view(['POST'])
@permission_classes([AllowAny])
def register_view(request):
    """
    API endpoint for user registration.

    Creates a new user account with the provided email, password, and profile information.
    """
    serializer = RegisterSerializer(data=request.data, context={"request": request})
    serializer.is_valid(raise_exception=True)
    user = serializer.save()

    # Return user data with a 201 Created status
    return Response(
        {"user": UserSerializer(user, context={"request": request}).data},
        status=status.HTTP_201_CREATED,
    )


@extend_schema(
    request=LoginSerializer,
    responses=LoginSerializer,
    tags=["Auth"],
    methods=["POST"]
)
@api_view(['POST'])
@permission_classes([AllowAny])
def login_view(request):
    """
    API endpoint for user authentication.

    Authenticates a user with email and password, returning JWT tokens and user data.
    """
    serializer = LoginSerializer(data=request.data, context={"request": request})
    serializer.is_valid(raise_exception=True)
    return Response(serializer.validated_data, status=status.HTTP_200_OK)


@extend_schema(
    request=PasswordResetRequestSerializer,
    responses={"200": None},
    tags=["Auth"],
    methods=["POST"]
)
@api_view(['POST'])
@permission_classes([AllowAny])
def password_reset_request_view(request):
    """
    API endpoint to request a password reset.

    Sends a password reset email to the provided email address if a user exists.
    """
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


@extend_schema(
    request=PasswordResetConfirmSerializer,
    responses={"200": None},
    tags=["Auth"],
    methods=["POST"]
)
@api_view(['POST'])
@permission_classes([AllowAny])
def password_reset_confirm_view(request):
    """
    API endpoint to confirm a password reset.

    Validates the reset token and sets a new password for the user.
    """
    serializer = PasswordResetConfirmSerializer(data=request.data, context={"request": request})
    serializer.is_valid(raise_exception=True)

    user = serializer.validated_data["user_obj"]
    new_password = serializer.validated_data["new_password"]

    # Set new password and save
    user.set_password(new_password)
    user.save(update_fields=["password"])

    return Response({"detail": "Password has been reset successfully."})


@extend_schema(
    request=ChangePasswordSerializer,
    responses={"200": None},
    tags=["Auth"],
    methods=["POST"]
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def change_password_view(request):
    """
    API endpoint for authenticated users to change their password.

    Requires the current password and a new password.
    """
    serializer = ChangePasswordSerializer(data=request.data, context={"request": request})
    serializer.is_valid(raise_exception=True)

    # Update the user's password
    user = request.user
    user.set_password(serializer.validated_data["new_password"])
    user.save(update_fields=["password"])

    return Response({"detail": "Password changed successfully."})


@extend_schema(
    responses=UserSerializer,
    tags=["Auth"],
    methods=["GET"]
)
@extend_schema(
    request=UserSerializer,
    responses=UserSerializer,
    tags=["Auth"],
    methods=["PUT", "PATCH"]
)
@api_view(['GET', 'PUT', 'PATCH'])
@permission_classes([IsAuthenticated])
def me_view(request):
    """
    API endpoint for retrieving and updating the authenticated user's profile.

    Provides GET, PUT, and PATCH methods for user profile management.
    """
    # Get current user with prefetched org memberships
    user = request.user
    user.update_last_active()

    # Prefetch active organization memberships to optimize the serializer
    user_obj = User.objects.filter(id=user.id).prefetch_related(
        Prefetch(
            "memberships",  # Using correct related_name from OrgMembership model
            queryset=OrgMembership.objects.filter(is_active=True).select_related("organization"),
            to_attr="active_memberships",
        )
    ).first()

    if request.method == 'GET':
        serializer = UserSerializer(user_obj, context={"request": request})
        return Response(serializer.data)

    elif request.method in ['PUT', 'PATCH']:
        partial = request.method == 'PATCH'
        serializer = UserSerializer(
            user_obj,
            data=request.data,
            partial=partial,
            context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


@extend_schema(
    responses={"200": None},
    tags=["Auth"],
    methods=["GET"]
)
@api_view(['GET'])
@permission_classes([AllowAny])
def verify_email_view(request, uidb64, token):
    """
    API endpoint to verify a user's email address.

    This would typically be called from a link in a verification email.
    """
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
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


@extend_schema(
    request=RefreshTokenSerializer,
    responses=RefreshTokenSerializer,
    tags=["Auth"],
    methods=["POST"]
)
@api_view(['POST'])
@permission_classes([AllowAny])
def refresh_token_view(request):
    """
    API endpoint for refreshing JWT access tokens.

    Takes a valid refresh token and returns a new access token and refresh token.
    """
    serializer = RefreshTokenSerializer(data=request.data, context={"request": request})
    serializer.is_valid(raise_exception=True)
    return Response(serializer.validated_data, status=status.HTTP_200_OK)


# User Management CRUD Views
@extend_schema(
    responses=UserSerializer(many=True),
    tags=["User Management"],
    methods=["GET"]
)
@extend_schema(
    request=UserSerializer,
    responses=UserSerializer,
    tags=["User Management"],
    methods=["POST"]
)
@api_view(['GET', 'POST'])
@permission_classes([IsSuperAdmin])
def user_list_view(request):
    """
    List all users or create a new user.
    Only super admins can access this endpoint.
    """
    if request.method == 'GET':
        users = User.objects.all().select_related().prefetch_related(
            Prefetch('memberships', queryset=OrgMembership.objects.select_related('organization'))
        )
        serializer = UserSerializer(users, many=True, context={"request": request})
        return Response(serializer.data)

    elif request.method == 'POST':
        serializer = UserSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(UserSerializer(user, context={"request": request}).data, status=status.HTTP_201_CREATED)


@extend_schema(
    responses=UserSerializer,
    tags=["User Management"],
    methods=["GET"]
)
@api_view(['GET'])
@permission_classes([IsSuperAdmin])
def user_detail_view(request, user_id):
    """
    Retrieve a specific user by ID.
    Only super admins can access this endpoint.
    """
    try:
        user = User.objects.select_related().prefetch_related(
            Prefetch('memberships', queryset=OrgMembership.objects.select_related('organization'))
        ).get(id=user_id)
    except User.DoesNotExist:
        return Response({"detail": "User not found."}, status=status.HTTP_404_NOT_FOUND)

    serializer = UserSerializer(user, context={"request": request})
    return Response(serializer.data)


@extend_schema(
    request=UserSerializer,
    responses=UserSerializer,
    tags=["User Management"],
    methods=["PUT", "PATCH"]
)
@api_view(['PUT', 'PATCH'])
@permission_classes([IsSuperAdmin])
def user_update_view(request, user_id):
    """
    Update a specific user by ID.
    Only super admins can access this endpoint.
    """
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return Response({"detail": "User not found."}, status=status.HTTP_404_NOT_FOUND)

    partial = request.method == 'PATCH'
    serializer = UserSerializer(user, data=request.data, partial=partial, context={"request": request})
    serializer.is_valid(raise_exception=True)
    serializer.save()
    return Response(serializer.data)


@extend_schema(
    responses={"204": None},
    tags=["User Management"],
    methods=["DELETE"]
)
@api_view(['DELETE'])
@permission_classes([IsSuperAdmin])
def user_delete_view(request, user_id):
    """
    Delete a specific user by ID.
    Only super admins can access this endpoint.
    """
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return Response({"detail": "User not found."}, status=status.HTTP_404_NOT_FOUND)

    user.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)

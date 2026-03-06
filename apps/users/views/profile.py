# apps/users/views/profile.py
"""
User profile & admin user management views.
"""
from django.contrib.auth import get_user_model
from django.db.models import Prefetch
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.permissions import IsSuperAdmin
from apps.organizations.models import OrgMembership
from apps.users.serializers import UserSerializer

User = get_user_model()


@extend_schema(responses=UserSerializer, tags=["Auth"], methods=["GET"])
@extend_schema(request=UserSerializer, responses=UserSerializer, tags=["Auth"], methods=["PUT", "PATCH"])
@api_view(["GET", "PUT", "PATCH"])
@permission_classes([IsAuthenticated])
def me_view(request):
    """Retrieve and update the authenticated user's profile."""
    user = request.user
    user.update_last_active()

    user_obj = (
        User.objects.filter(id=user.id)
        .prefetch_related(
            Prefetch(
                "memberships",
                queryset=OrgMembership.objects.filter(is_active=True).select_related("organization"),
                to_attr="active_memberships",
            )
        )
        .first()
    )

    if request.method == "GET":
        serializer = UserSerializer(user_obj, context={"request": request})
        return Response(serializer.data)

    partial = request.method == "PATCH"
    serializer = UserSerializer(user_obj, data=request.data, partial=partial, context={"request": request})
    serializer.is_valid(raise_exception=True)
    serializer.save()
    return Response(serializer.data)


# ---------------------------------------------------------------------------
# Super-admin user management
# ---------------------------------------------------------------------------

@extend_schema(responses=UserSerializer(many=True), tags=["User Management"], methods=["GET"])
@extend_schema(request=UserSerializer, responses=UserSerializer, tags=["User Management"], methods=["POST"])
@api_view(["GET", "POST"])
@permission_classes([IsSuperAdmin])
def user_list_view(request):
    """List all users or create a new user. Super admins only."""
    if request.method == "GET":
        users = User.objects.all().select_related().prefetch_related(
            Prefetch('memberships', queryset=OrgMembership.objects.select_related('organization'))
        )
        serializer = UserSerializer(users, many=True, context={"request": request})
        return Response(serializer.data)

    serializer = UserSerializer(data=request.data, context={"request": request})
    serializer.is_valid(raise_exception=True)
    user = serializer.save()
    return Response(UserSerializer(user, context={"request": request}).data, status=status.HTTP_201_CREATED)


@extend_schema(responses=UserSerializer, tags=["User Management"], methods=["GET"])
@api_view(["GET"])
@permission_classes([IsSuperAdmin])
def user_detail_view(request, user_id):
    """Retrieve a specific user by ID. Super admins only."""
    try:
        user = User.objects.select_related().prefetch_related(
            Prefetch('memberships', queryset=OrgMembership.objects.select_related('organization'))
        ).get(id=user_id)
    except User.DoesNotExist:
        return Response({"detail": "User not found."}, status=status.HTTP_404_NOT_FOUND)
    return Response(UserSerializer(user, context={"request": request}).data)


@extend_schema(request=UserSerializer, responses=UserSerializer, tags=["User Management"], methods=["PUT", "PATCH"])
@api_view(["PUT", "PATCH"])
@permission_classes([IsSuperAdmin])
def user_update_view(request, user_id):
    """Update a specific user by ID. Super admins only."""
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return Response({"detail": "User not found."}, status=status.HTTP_404_NOT_FOUND)

    partial = request.method == "PATCH"
    serializer = UserSerializer(user, data=request.data, partial=partial, context={"request": request})
    serializer.is_valid(raise_exception=True)
    serializer.save()
    return Response(serializer.data)


@extend_schema(responses={"204": None}, tags=["User Management"], methods=["DELETE"])
@api_view(["DELETE"])
@permission_classes([IsSuperAdmin])
def user_delete_view(request, user_id):
    """Delete a specific user by ID. Super admins only."""
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return Response({"detail": "User not found."}, status=status.HTTP_404_NOT_FOUND)
    user.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)

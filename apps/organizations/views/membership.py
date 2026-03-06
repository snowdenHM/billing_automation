# apps/organizations/views/membership.py
"""
Organization membership management views.
"""
from datetime import datetime

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.utils.crypto import get_random_string
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied, ValidationError
from drf_spectacular.utils import extend_schema

from apps.organizations.models import Organization, OrgMembership
from apps.organizations.serializers import (
    OrgMembershipSerializer,
    OrgMembershipUpdateSerializer,
)

User = get_user_model()


# ---------------------------------------------------------------------------
# Org-scoped member management
# ---------------------------------------------------------------------------

@extend_schema(
    request=OrgMembershipSerializer,
    responses=OrgMembershipSerializer,
    tags=["Organizations"],
    methods=["POST"],
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def organization_invite_user_view(request, org_id):
    """
    Invite a user to an organization by email.
    Creates user if doesn't exist, then creates organization membership.
    """
    try:
        organization = Organization.objects.get(pk=org_id)
    except Organization.DoesNotExist:
        return Response({"detail": "Organization not found."}, status=status.HTTP_404_NOT_FOUND)

    if not (
        request.user.is_staff
        or organization.memberships.filter(user=request.user, role="ADMIN", is_active=True).exists()
    ):
        raise PermissionDenied("You don't have permission to add members to this organization")

    user_email = request.data.get("email")
    user_role = request.data.get("role", "MANAGER")
    first_name = request.data.get("first_name", "")
    last_name = request.data.get("last_name", "")
    full_name = request.data.get("full_name", "")

    if not user_email:
        return Response({"detail": "User email is required."}, status=status.HTTP_400_BAD_REQUEST)

    # Split full_name if provided but no first/last names
    if full_name and not (first_name or last_name):
        name_parts = full_name.strip().split()
        first_name = name_parts[0] if name_parts else ""
        last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

    # Check if user exists, create if not
    try:
        user_to_add = User.objects.get(email=user_email)
        user_created = False
    except User.DoesNotExist:
        default_password = get_random_string(length=20)
        if not first_name:
            first_name = user_email.split("@")[0]

        user_to_add = User.objects.create(
            email=user_email,
            username=user_email,
            first_name=first_name,
            last_name=last_name,
            password=make_password(default_password),
            is_active=True,
        )
        user_created = True

    # Check if user is already a member
    existing_membership = OrgMembership.objects.filter(
        organization=organization, user=user_to_add
    ).first()

    if existing_membership:
        if existing_membership.is_active:
            return Response(
                {"detail": "User is already a member of this organization."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Reactivate existing membership
        existing_membership.is_active = True
        existing_membership.role = user_role
        existing_membership.save()
        serializer = OrgMembershipSerializer(existing_membership, context={"request": request})
        message = "User membership reactivated."
        if user_created:
            message += " New user created with default password."
        return Response(
            {"data": serializer.data, "message": message, "user_created": user_created},
            status=status.HTTP_200_OK,
        )

    # Create new membership
    membership = OrgMembership.objects.create(
        organization=organization, user=user_to_add, role=user_role, is_active=True
    )
    serializer = OrgMembershipSerializer(membership, context={"request": request})

    current_year = datetime.now().year
    message = "User successfully added to organization."
    if user_created:
        message += " New user created with a temporary password. Please ask them to reset it."

    return Response(
        {
            "data": serializer.data,
            "message": message,
            "user_created": user_created,
        },
        status=status.HTTP_201_CREATED,
    )


@extend_schema(
    request=OrgMembershipSerializer,
    responses=OrgMembershipSerializer,
    tags=["Organizations"],
    methods=["POST"],
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def organization_add_member_view(request, org_id):
    """Add a member to an organization."""
    try:
        organization = Organization.objects.get(pk=org_id)
    except Organization.DoesNotExist:
        return Response({"detail": "Organization not found."}, status=status.HTTP_404_NOT_FOUND)

    if not (
        request.user.is_staff
        or organization.memberships.filter(user=request.user, role="ADMIN", is_active=True).exists()
    ):
        raise PermissionDenied("You don't have permission to add members to this organization")

    serializer = OrgMembershipSerializer(
        data={**request.data, "organization": organization.id},
        context={"request": request},
    )
    serializer.is_valid(raise_exception=True)
    serializer.save()
    return Response({"data": serializer.data}, status=status.HTTP_201_CREATED)


@extend_schema(
    responses=OrgMembershipSerializer(many=True),
    tags=["Organizations"],
    methods=["GET"],
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def organization_members_view(request, org_id):
    """List members of an organization."""
    try:
        organization = Organization.objects.get(pk=org_id)
    except Organization.DoesNotExist:
        return Response({"detail": "Organization not found."}, status=status.HTTP_404_NOT_FOUND)

    if not request.user.is_staff:
        if not organization.memberships.filter(user=request.user, is_active=True).exists():
            raise PermissionDenied("You don't have access to this organization")

    queryset = (
        OrgMembership.objects.filter(organization=organization, is_active=True)
        .select_related("user", "organization")
        .order_by("-created_at")
    )
    serializer = OrgMembershipSerializer(queryset, many=True, context={"request": request})

    return Response({
        "data": {
            "organization": {
                "id": str(organization.id),
                "name": organization.name,
                "unique_name": organization.unique_name,
            },
            "members": serializer.data,
            "meta": {
                "total_members": len(serializer.data),
                "active_members": len(serializer.data),
                "roles_breakdown": {
                    "admins": len([m for m in serializer.data if m["role"] == "ADMIN"]),
                    "members": len([m for m in serializer.data if m["role"] == "MEMBER"]),
                },
            },
        }
    })


@extend_schema(responses={"204": None}, tags=["Organizations"], methods=["DELETE"])
@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def organization_remove_member_view(request, org_id, membership_id):
    """Remove (deactivate) a member from the organization."""
    try:
        organization = Organization.objects.get(pk=org_id)
    except Organization.DoesNotExist:
        return Response({"detail": "Organization not found."}, status=status.HTTP_404_NOT_FOUND)

    if not (
        request.user.is_staff
        or organization.memberships.filter(user=request.user, role="ADMIN", is_active=True).exists()
    ):
        raise PermissionDenied("You don't have permission to remove members from this organization")

    try:
        membership = OrgMembership.objects.get(organization=organization, id=membership_id, is_active=True)
    except OrgMembership.DoesNotExist:
        return Response({"detail": "Membership not found."}, status=status.HTTP_404_NOT_FOUND)

    if membership.user == organization.owner:
        return Response({"detail": "Cannot remove organization owner"}, status=status.HTTP_400_BAD_REQUEST)

    membership.is_active = False
    membership.save(update_fields=["is_active"])
    return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(responses={"204": None}, tags=["Organizations"], methods=["DELETE"])
@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def organization_delete_member_view(request, org_id, membership_id):
    """
    Delete a member from the organization and optionally delete user account.
    Query params: delete_user=true to also delete the user account.
    """
    try:
        organization = Organization.objects.get(pk=org_id)
    except Organization.DoesNotExist:
        return Response({"detail": "Organization not found."}, status=status.HTTP_404_NOT_FOUND)

    if not (
        request.user.is_staff
        or organization.memberships.filter(user=request.user, role="ADMIN", is_active=True).exists()
    ):
        raise PermissionDenied("You don't have permission to delete members from this organization")

    try:
        membership = OrgMembership.objects.get(organization=organization, id=membership_id, is_active=True)
    except OrgMembership.DoesNotExist:
        return Response({"detail": "Membership not found."}, status=status.HTTP_404_NOT_FOUND)

    if membership.user == organization.owner:
        return Response({"detail": "Cannot delete organization owner"}, status=status.HTTP_400_BAD_REQUEST)

    user_to_delete = membership.user
    delete_user_account = request.query_params.get("delete_user", "false").lower() == "true"

    membership.delete()
    response_message = "Member removed from organization."

    if delete_user_account:
        other_memberships = OrgMembership.objects.filter(user=user_to_delete, is_active=True).exists()
        if not other_memberships:
            user_to_delete.delete()
            response_message += " User account also deleted."
        else:
            response_message += " User account kept due to other active memberships."

    return Response({"message": response_message}, status=status.HTTP_200_OK)


@extend_schema(
    request=OrgMembershipUpdateSerializer,
    responses=OrgMembershipSerializer,
    tags=["Organizations"],
    methods=["PATCH"],
)
@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def organization_update_member_role_view(request, org_id, membership_id):
    """Update a member's role in the organization."""
    try:
        organization = Organization.objects.get(pk=org_id)
    except Organization.DoesNotExist:
        return Response({"detail": "Organization not found."}, status=status.HTTP_404_NOT_FOUND)

    if not (
        request.user.is_staff
        or organization.memberships.filter(user=request.user, role="ADMIN", is_active=True).exists()
    ):
        raise PermissionDenied("You don't have permission to update member roles in this organization")

    try:
        membership = OrgMembership.objects.get(organization=organization, id=membership_id, is_active=True)
    except OrgMembership.DoesNotExist:
        return Response({"detail": "Membership not found."}, status=status.HTTP_404_NOT_FOUND)

    if membership.user == organization.owner:
        return Response({"detail": "Cannot change organization owner's role"}, status=status.HTTP_400_BAD_REQUEST)

    serializer = OrgMembershipUpdateSerializer(membership, data=request.data, partial=True, context={"request": request})
    serializer.is_valid(raise_exception=True)
    serializer.save()
    return Response({"data": OrgMembershipSerializer(membership, context={"request": request}).data})


# ---------------------------------------------------------------------------
# Generic membership CRUD (not org-scoped)
# ---------------------------------------------------------------------------

@extend_schema(
    responses=OrgMembershipSerializer(many=True),
    tags=["Organization Memberships"],
    methods=["GET"],
)
@extend_schema(
    request=OrgMembershipSerializer,
    responses=OrgMembershipSerializer,
    tags=["Organization Memberships"],
    methods=["POST"],
)
@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def membership_list_view(request):
    """List or create organization memberships."""
    if request.method == "GET":
        if request.user.is_staff:
            memberships = OrgMembership.objects.all().select_related("user", "organization")
        else:
            user_org_ids = request.user.memberships.filter(is_active=True).values_list("organization_id", flat=True)
            memberships = OrgMembership.objects.filter(
                organization_id__in=user_org_ids, is_active=True
            ).select_related("user", "organization")

        serializer = OrgMembershipSerializer(memberships, many=True, context={"request": request})
        return Response({"data": serializer.data})

    # POST
    serializer = OrgMembershipSerializer(data=request.data, context={"request": request})
    serializer.is_valid(raise_exception=True)

    organization = serializer.validated_data["organization"]
    if not (
        request.user.is_staff
        or organization.memberships.filter(user=request.user, role="ADMIN", is_active=True).exists()
    ):
        raise PermissionDenied("You don't have permission to add members to this organization")

    serializer.save()
    return Response({"data": serializer.data}, status=status.HTTP_201_CREATED)


@extend_schema(responses=OrgMembershipSerializer, tags=["Organization Memberships"], methods=["GET"])
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def membership_detail_view(request, pk):
    """Retrieve a specific membership."""
    try:
        if request.user.is_staff:
            membership = OrgMembership.objects.select_related("user", "organization").get(pk=pk)
        else:
            user_org_ids = request.user.memberships.filter(is_active=True).values_list("organization_id", flat=True)
            membership = OrgMembership.objects.select_related("user", "organization").get(
                pk=pk, organization_id__in=user_org_ids
            )
    except OrgMembership.DoesNotExist:
        return Response({"detail": "Membership not found."}, status=status.HTTP_404_NOT_FOUND)

    serializer = OrgMembershipSerializer(membership, context={"request": request})
    return Response({"data": serializer.data})


@extend_schema(
    request=OrgMembershipSerializer,
    responses=OrgMembershipSerializer,
    tags=["Organization Memberships"],
    methods=["PUT", "PATCH"],
)
@api_view(["PUT", "PATCH"])
@permission_classes([IsAuthenticated])
def membership_update_view(request, pk):
    """Update a membership."""
    try:
        membership = OrgMembership.objects.get(pk=pk)
    except OrgMembership.DoesNotExist:
        return Response({"detail": "Membership not found."}, status=status.HTTP_404_NOT_FOUND)

    if not (
        request.user.is_staff
        or membership.organization.memberships.filter(user=request.user, role="ADMIN", is_active=True).exists()
    ):
        raise PermissionDenied("You don't have permission to update this membership")

    partial = request.method == "PATCH"
    serializer = OrgMembershipSerializer(membership, data=request.data, partial=partial, context={"request": request})
    serializer.is_valid(raise_exception=True)
    serializer.save()
    return Response({"data": serializer.data})


@extend_schema(responses={"204": None}, tags=["Organization Memberships"], methods=["DELETE"])
@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def membership_delete_view(request, pk):
    """Delete (deactivate) a membership."""
    try:
        membership = OrgMembership.objects.get(pk=pk)
    except OrgMembership.DoesNotExist:
        return Response({"detail": "Membership not found."}, status=status.HTTP_404_NOT_FOUND)

    if not (
        request.user.is_staff
        or membership.organization.memberships.filter(user=request.user, role="ADMIN", is_active=True).exists()
    ):
        raise PermissionDenied("You don't have permission to delete this membership")

    if membership.user == membership.organization.owner:
        raise ValidationError("Cannot remove organization owner")

    membership.is_active = False
    membership.save(update_fields=["is_active"])
    return Response(status=status.HTTP_204_NO_CONTENT)

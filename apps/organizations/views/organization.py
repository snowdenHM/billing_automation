# apps/organizations/views/organization.py
"""
Organization CRUD views.
"""
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied
from drf_spectacular.utils import extend_schema

from apps.organizations.models import Organization
from apps.organizations.serializers import OrganizationSerializer


@extend_schema(
    responses=OrganizationSerializer(many=True),
    tags=["Organizations"],
    methods=["GET"],
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def organization_list_view(request):
    """List organizations where the authenticated user has an active membership."""
    user_org_ids = request.user.memberships.filter(
        is_active=True
    ).values_list("organization_id", flat=True)

    organizations = Organization.objects.filter(
        id__in=user_org_ids
    ).select_related("owner", "created_by")

    serializer = OrganizationSerializer(organizations, many=True, context={"request": request})
    return Response({"data": serializer.data})


@extend_schema(
    responses=OrganizationSerializer,
    tags=["Organizations"],
    methods=["GET"],
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def organization_detail_view(request, pk):
    """Retrieve a specific organization by ID."""
    try:
        organization = Organization.objects.select_related("owner", "created_by").get(pk=pk)
    except Organization.DoesNotExist:
        return Response({"detail": "Organization not found."}, status=status.HTTP_404_NOT_FOUND)

    if not request.user.is_staff:
        if not organization.memberships.filter(user=request.user, is_active=True).exists():
            raise PermissionDenied("You don't have access to this organization")

    serializer = OrganizationSerializer(organization, context={"request": request})
    return Response({"data": serializer.data})


@extend_schema(
    request=OrganizationSerializer,
    responses=OrganizationSerializer,
    tags=["Organizations"],
    methods=["PUT", "PATCH"],
)
@api_view(["PUT", "PATCH"])
@permission_classes([IsAuthenticated])
def organization_update_view(request, pk):
    """Update an organization. Only org admins can update."""
    try:
        organization = Organization.objects.get(pk=pk)
    except Organization.DoesNotExist:
        return Response({"detail": "Organization not found."}, status=status.HTTP_404_NOT_FOUND)

    if not (
        request.user.is_staff
        or organization.memberships.filter(user=request.user, role="ADMIN", is_active=True).exists()
    ):
        raise PermissionDenied("You don't have permission to update this organization")

    partial = request.method == "PATCH"
    serializer = OrganizationSerializer(
        organization, data=request.data, partial=partial, context={"request": request}
    )
    serializer.is_valid(raise_exception=True)
    serializer.save()
    return Response({"data": serializer.data})

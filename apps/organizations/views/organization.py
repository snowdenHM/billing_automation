# apps/organizations/views/organization.py
"""
Organization CRUD views.
"""
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from drf_spectacular.utils import extend_schema
from django.shortcuts import get_object_or_404

from apps.common.permissions import assert_org_access
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
    organization = get_object_or_404(
        Organization.objects.select_related("owner", "created_by"), pk=pk
    )
    assert_org_access(request.user, organization)

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
    organization = get_object_or_404(Organization, pk=pk)
    assert_org_access(request.user, organization, admin_only=True)

    partial = request.method == "PATCH"
    serializer = OrganizationSerializer(
        organization, data=request.data, partial=partial, context={"request": request}
    )
    serializer.is_valid(raise_exception=True)
    serializer.save()
    return Response({"data": serializer.data})

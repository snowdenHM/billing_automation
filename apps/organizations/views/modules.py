# apps/organizations/views/modules.py
"""
Organization module management views (function-based).
"""
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied
from drf_spectacular.utils import extend_schema

from apps.organizations.models import (
    Organization,
    Module,
    OrganizationModule,
)
from apps.organizations.serializers import (
    ModuleSerializer,
    OrganizationModuleSerializer,
)


@extend_schema(
    responses=OrganizationModuleSerializer(many=True),
    tags=["Organizations"],
    methods=["GET"],
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def organization_modules_view(request, org_id):
    """List modules enabled for the organization."""
    try:
        organization = Organization.objects.get(pk=org_id)
    except Organization.DoesNotExist:
        return Response({"detail": "Organization not found."}, status=status.HTTP_404_NOT_FOUND)

    if not request.user.is_staff:
        if not organization.memberships.filter(user=request.user, is_active=True).exists():
            raise PermissionDenied("You don't have access to this organization")

    queryset = OrganizationModule.objects.filter(organization=organization).select_related(
        "organization", "organization__owner", "organization__created_by", "module"
    )
    serializer = OrganizationModuleSerializer(queryset, many=True, context={"request": request})
    return Response(serializer.data)


@extend_schema(
    responses=OrganizationModuleSerializer,
    tags=["Organizations"],
    methods=["POST"],
)
@extend_schema(
    responses={"204": None},
    tags=["Organizations"],
    methods=["DELETE"],
)
@api_view(["POST", "DELETE"])
@permission_classes([IsAuthenticated])
def organization_set_module_view(request, org_id, code):
    """Enable or disable a module for the organization."""
    try:
        organization = Organization.objects.get(pk=org_id)
    except Organization.DoesNotExist:
        return Response({"detail": "Organization not found."}, status=status.HTTP_404_NOT_FOUND)

    if not (
        request.user.is_staff
        or organization.memberships.filter(user=request.user, role="ADMIN", is_active=True).exists()
    ):
        raise PermissionDenied("You don't have permission to manage modules for this organization")

    try:
        module = Module.objects.get(code=code)
    except Module.DoesNotExist:
        return Response({"detail": "Module not found."}, status=status.HTTP_404_NOT_FOUND)

    if request.method == "DELETE":
        OrganizationModule.objects.filter(organization=organization, module=module).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    obj, _ = OrganizationModule.objects.get_or_create(
        organization=organization,
        module=module,
        defaults={"is_active": True},
    )
    serializer = OrganizationModuleSerializer(obj, context={"request": request})
    return Response({"data": serializer.data})


@extend_schema(
    responses=ModuleSerializer(many=True),
    tags=["Organizations"],
    methods=["GET"],
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def modules_catalog_view(request):
    """List all available modules that can be enabled."""
    queryset = Module.objects.all()
    serializer = ModuleSerializer(queryset, many=True, context={"request": request})
    return Response({"data": serializer.data})

# apps/organizations/views/onboarding.py
"""
Organization onboarding flow views.
"""
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied
from drf_spectacular.utils import extend_schema

from apps.organizations.models import (
    Organization,
    OrgMembership,
    Module,
    OrganizationModule,
)
from apps.organizations.serializers import (
    OrganizationSerializer,
    OrgMembershipSerializer,
    OrganizationModuleSerializer,
)


@extend_schema(
    request=OrganizationSerializer,
    responses=OrganizationSerializer,
    tags=["Organization Onboarding"],
    methods=["POST"],
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def organization_create_with_module_view(request):
    """
    Create organization, add user as admin member, and enable specified module.
    All in one shot – three operations combined.
    """
    module_code = request.data.get("module")
    if not module_code:
        return Response({"detail": "Module code is required (tally or zoho)."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        module = Module.objects.get(code=module_code)
    except Module.DoesNotExist:
        return Response({"detail": "Invalid module code."}, status=status.HTTP_400_BAD_REQUEST)

    org_name = request.data.get("name")
    if not org_name:
        return Response({"detail": "Organization name is required."}, status=status.HTTP_400_BAD_REQUEST)

    organization = Organization.objects.create(
        name=org_name,
        owner=request.user,
        created_by=request.user,
        status=Organization.ACTIVE,
    )
    membership = OrgMembership.objects.create(
        organization=organization, user=request.user, role=OrgMembership.ADMIN, is_active=True
    )
    org_module = OrganizationModule.objects.create(
        organization=organization, module=module, is_active=True
    )

    return Response(
        {
            "data": {
                "organization": OrganizationSerializer(organization, context={"request": request}).data,
                "membership": OrgMembershipSerializer(membership, context={"request": request}).data,
                "enabled_module": OrganizationModuleSerializer(org_module, context={"request": request}).data,
                "message": f"Organization created successfully with {module.name} module enabled.",
            }
        },
        status=status.HTTP_201_CREATED,
    )


@extend_schema(
    request=OrganizationSerializer,
    responses=OrganizationSerializer,
    tags=["Organization Onboarding"],
    methods=["POST"],
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def organization_onboarding_create_view(request):
    """Create a new organization with the requesting user as admin member."""
    serializer = OrganizationSerializer(data=request.data, context={"request": request})
    serializer.is_valid(raise_exception=True)

    organization = serializer.save(created_by=request.user, owner=request.user)

    OrgMembership.objects.create(
        organization=organization, user=request.user, role=OrgMembership.ADMIN, is_active=True
    )
    return Response({"data": serializer.data}, status=status.HTTP_201_CREATED)


@extend_schema(
    request=OrganizationModuleSerializer,
    responses=OrganizationModuleSerializer,
    tags=["Organization Onboarding"],
    methods=["POST"],
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def organization_onboarding_enable_module_view(request, org_id):
    """Enable a module for an organization during onboarding."""
    try:
        organization = Organization.objects.get(pk=org_id)
    except Organization.DoesNotExist:
        return Response({"detail": "Organization not found."}, status=status.HTTP_404_NOT_FOUND)

    if not (
        request.user.is_staff
        or organization.memberships.filter(user=request.user, role="ADMIN", is_active=True).exists()
    ):
        raise PermissionDenied("You don't have permission to enable modules for this organization")

    module_code = request.data.get("module")
    if not module_code:
        return Response({"detail": "Module code is required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        module = Module.objects.get(code=module_code)
    except Module.DoesNotExist:
        return Response({"detail": "Module not found."}, status=status.HTTP_404_NOT_FOUND)

    org_module, created = OrganizationModule.objects.get_or_create(
        organization=organization, module=module, defaults={"is_active": True}
    )

    if not created and not org_module.is_active:
        org_module.is_active = True
        org_module.save(update_fields=["is_active"])

    serializer = OrganizationModuleSerializer(org_module, context={"request": request})
    return Response({"data": serializer.data}, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

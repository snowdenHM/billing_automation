from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import PermissionDenied, ValidationError
from drf_spectacular.utils import extend_schema
from rest_framework_api_key.models import APIKey

from .models import (
    Organization,
    OrgMembership,
    OrganizationAPIKey,
    Module,
    OrganizationModule,
)
from .serializers import (
    OrganizationSerializer,
    OrgMembershipSerializer,
    APIKeyIssueSerializer,
    APIKeySerializer,
    ModuleSerializer,
    OrganizationModuleSerializer,
    OrgMembershipUpdateSerializer,
)

User = get_user_model()


# Organization Views
@extend_schema(
    responses=OrganizationSerializer(many=True),
    tags=["Organizations"],
    methods=["GET"]
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def organization_list_view(request):
    """
    List organizations accessible to the authenticated user.
    """
    # Users can only see organizations they belong to
    if request.user.is_staff:
        organizations = Organization.objects.all().select_related("owner", "created_by")
    else:
        user_org_ids = request.user.memberships.filter(
            is_active=True
        ).values_list('organization_id', flat=True)
        organizations = Organization.objects.filter(
            id__in=user_org_ids
        ).select_related("owner", "created_by")

    serializer = OrganizationSerializer(organizations, many=True, context={"request": request})
    return Response({"data": serializer.data})


@extend_schema(
    responses=OrganizationSerializer,
    tags=["Organizations"],
    methods=["GET"]
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def organization_detail_view(request, pk):
    """
    Retrieve a specific organization by ID.
    """
    try:
        organization = Organization.objects.select_related("owner", "created_by").get(pk=pk)
    except Organization.DoesNotExist:
        return Response({"detail": "Organization not found."}, status=status.HTTP_404_NOT_FOUND)

    # Check if user has access to this organization
    if not request.user.is_staff:
        if not organization.memberships.filter(user=request.user, is_active=True).exists():
            raise PermissionDenied("You don't have access to this organization")

    serializer = OrganizationSerializer(organization, context={"request": request})
    return Response({"data": serializer.data})


@extend_schema(
    request=OrganizationSerializer,
    responses=OrganizationSerializer,
    tags=["Organizations"],
    methods=["PUT", "PATCH"]
)
@api_view(['PUT', 'PATCH'])
@permission_classes([IsAuthenticated])
def organization_update_view(request, pk):
    """
    Update an organization. Only org admins can update.
    """
    try:
        organization = Organization.objects.get(pk=pk)
    except Organization.DoesNotExist:
        return Response({"detail": "Organization not found."}, status=status.HTTP_404_NOT_FOUND)

    # Check if user is admin of the organization
    if not (request.user.is_staff or
            organization.memberships.filter(
                user=request.user,
                role='ADMIN',
                is_active=True
            ).exists()):
        raise PermissionDenied("You don't have permission to update this organization")

    partial = request.method == 'PATCH'
    serializer = OrganizationSerializer(
        organization,
        data=request.data,
        partial=partial,
        context={"request": request}
    )
    serializer.is_valid(raise_exception=True)
    serializer.save()
    return Response({"data": serializer.data})


# Member Management Views
@extend_schema(
    request=OrgMembershipSerializer,
    responses=OrgMembershipSerializer,
    tags=["Organizations"],
    methods=["POST"]
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def organization_add_member_view(request, org_id):
    """
    Add a member to an organization.
    """
    try:
        organization = Organization.objects.get(pk=org_id)
    except Organization.DoesNotExist:
        return Response({"detail": "Organization not found."}, status=status.HTTP_404_NOT_FOUND)

    # Check if user is admin of the organization
    if not (request.user.is_staff or
            organization.memberships.filter(
                user=request.user,
                role='ADMIN',
                is_active=True
            ).exists()):
        raise PermissionDenied("You don't have permission to add members to this organization")

    serializer = OrgMembershipSerializer(
        data={**request.data, "organization": organization.id},
        context={"request": request}
    )
    serializer.is_valid(raise_exception=True)
    serializer.save()
    return Response({"data": serializer.data}, status=status.HTTP_201_CREATED)


@extend_schema(
    responses=OrgMembershipSerializer(many=True),
    tags=["Organizations"],
    methods=["GET"]
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def organization_members_view(request, org_id):
    """
    List members of an organization.
    """
    try:
        organization = Organization.objects.get(pk=org_id)
    except Organization.DoesNotExist:
        return Response({"detail": "Organization not found."}, status=status.HTTP_404_NOT_FOUND)

    # Check if user has access to this organization
    if not request.user.is_staff:
        if not organization.memberships.filter(user=request.user, is_active=True).exists():
            raise PermissionDenied("You don't have access to this organization")

    queryset = OrgMembership.objects.filter(
        organization=organization, is_active=True
    ).select_related("user", "organization").order_by('-created_at')

    serializer = OrgMembershipSerializer(queryset, many=True, context={"request": request})

    # Enhanced response format with metadata
    response_data = {
        "data": {
            "organization": {
                "id": str(organization.id),
                "name": organization.name,
                "unique_name": organization.unique_name
            },
            "members": serializer.data,
            "meta": {
                "total_members": len(serializer.data),
                "active_members": len(serializer.data),
                "roles_breakdown": {
                    "admins": len([m for m in serializer.data if m['role'] == 'ADMIN']),
                    "members": len([m for m in serializer.data if m['role'] == 'MEMBER'])
                }
            }
        }
    }

    return Response(response_data)


@extend_schema(
    responses={"204": None},
    tags=["Organizations"],
    methods=["DELETE"]
)
@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def organization_remove_member_view(request, org_id, membership_id):
    """
    Remove a member from the organization.
    """
    try:
        organization = Organization.objects.get(pk=org_id)
    except Organization.DoesNotExist:
        return Response({"detail": "Organization not found."}, status=status.HTTP_404_NOT_FOUND)

    # Check if user is admin of the organization
    if not (request.user.is_staff or
            organization.memberships.filter(
                user=request.user,
                role='ADMIN',
                is_active=True
            ).exists()):
        raise PermissionDenied("You don't have permission to remove members from this organization")

    try:
        membership = OrgMembership.objects.get(
            organization=organization,
            id=membership_id,
            is_active=True
        )
    except OrgMembership.DoesNotExist:
        return Response({"detail": "Membership not found."}, status=status.HTTP_404_NOT_FOUND)

    # Prevent removing the organization owner
    if membership.user == organization.owner:
        return Response(
            {"detail": "Cannot remove organization owner"},
            status=status.HTTP_400_BAD_REQUEST
        )

    membership.is_active = False
    membership.save(update_fields=['is_active'])
    return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(
    request=OrgMembershipUpdateSerializer,
    responses=OrgMembershipSerializer,
    tags=["Organizations"],
    methods=["PATCH"]
)
@api_view(['PATCH'])
@permission_classes([IsAuthenticated])
def organization_update_member_role_view(request, org_id, membership_id):
    """
    Update a member's role in the organization.
    """
    try:
        organization = Organization.objects.get(pk=org_id)
    except Organization.DoesNotExist:
        return Response({"detail": "Organization not found."}, status=status.HTTP_404_NOT_FOUND)

    # Check if user is admin of the organization
    if not (request.user.is_staff or
            organization.memberships.filter(
                user=request.user,
                role='ADMIN',
                is_active=True
            ).exists()):
        raise PermissionDenied("You don't have permission to update member roles in this organization")

    try:
        membership = OrgMembership.objects.get(
            organization=organization,
            id=membership_id,
            is_active=True
        )
    except OrgMembership.DoesNotExist:
        return Response({"detail": "Membership not found."}, status=status.HTTP_404_NOT_FOUND)

    # Prevent changing the organization owner's role
    if membership.user == organization.owner:
        return Response(
            {"detail": "Cannot change organization owner's role"},
            status=status.HTTP_400_BAD_REQUEST
        )

    serializer = OrgMembershipUpdateSerializer(
        membership,
        data=request.data,
        partial=True,
        context={"request": request}
    )
    serializer.is_valid(raise_exception=True)
    serializer.save()
    return Response({"data": OrgMembershipSerializer(membership, context={"request": request}).data})


# API Key Management Views
@extend_schema(
    request=APIKeyIssueSerializer,
    responses=APIKeySerializer,
    tags=["Organizations"],
    methods=["POST"]
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def organization_issue_api_key_view(request, org_id):
    """
    Issue a new API key for the organization.
    """
    try:
        organization = Organization.objects.get(pk=org_id)
    except Organization.DoesNotExist:
        return Response({"detail": "Organization not found."}, status=status.HTTP_404_NOT_FOUND)

    # Check if user is admin of the organization
    if not (request.user.is_staff or
            organization.memberships.filter(
                user=request.user,
                role='ADMIN',
                is_active=True
            ).exists()):
        raise PermissionDenied("You don't have permission to issue API keys for this organization")

    serializer = APIKeyIssueSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    api_key, key = APIKey.objects.create_key(name=serializer.validated_data["name"])
    org_api_key = OrganizationAPIKey.objects.create(
        api_key=api_key,
        organization=organization,
        name=serializer.validated_data["name"],
        created_by=request.user,
    )

    return Response(
        {
            "data": {
                **APIKeySerializer(org_api_key, context={"request": request}).data,
                "key": key,  # Include the actual key in response
            }
        },
        status=status.HTTP_201_CREATED,
    )


@extend_schema(
    responses=APIKeySerializer(many=True),
    tags=["Organizations"],
    methods=["GET"]
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def organization_list_api_keys_view(request, org_id):
    """
    List API keys for the organization.
    """
    try:
        organization = Organization.objects.get(pk=org_id)
    except Organization.DoesNotExist:
        return Response({"detail": "Organization not found."}, status=status.HTTP_404_NOT_FOUND)

    # Check if user is admin of the organization
    if not (request.user.is_staff or
            organization.memberships.filter(
                user=request.user,
                role='ADMIN',
                is_active=True
            ).exists()):
        raise PermissionDenied("You don't have permission to view API keys for this organization")

    queryset = OrganizationAPIKey.objects.filter(
        organization=organization
    ).select_related("created_by", "organization", "organization__owner", "organization__created_by")
    serializer = APIKeySerializer(queryset, many=True, context={"request": request})
    return Response(serializer.data)


@extend_schema(
    responses=APIKeySerializer,
    tags=["Organizations"],
    methods=["POST"]
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def organization_revoke_api_key_view(request, org_id, key_id):
    """
    Revoke an API key for the organization.
    """
    try:
        organization = Organization.objects.get(pk=org_id)
    except Organization.DoesNotExist:
        return Response({"detail": "Organization not found."}, status=status.HTTP_404_NOT_FOUND)

    # Check if user is admin of the organization
    if not (request.user.is_staff or
            organization.memberships.filter(
                user=request.user,
                role='ADMIN',
                is_active=True
            ).exists()):
        raise PermissionDenied("You don't have permission to revoke API keys for this organization")

    try:
        api_key = OrganizationAPIKey.objects.get(organization=organization, id=key_id)
    except OrganizationAPIKey.DoesNotExist:
        return Response({"detail": "API key not found."}, status=status.HTTP_404_NOT_FOUND)

    api_key.api_key.revoked = True
    api_key.api_key.save()
    return Response({"data": APIKeySerializer(api_key, context={"request": request}).data})


# Module Management Views
@extend_schema(
    responses=OrganizationModuleSerializer(many=True),
    tags=["Organizations"],
    methods=["GET"]
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def organization_modules_view(request, org_id):
    """
    List modules enabled for the organization.
    """
    try:
        organization = Organization.objects.get(pk=org_id)
    except Organization.DoesNotExist:
        return Response({"detail": "Organization not found."}, status=status.HTTP_404_NOT_FOUND)

    # Check if user has access to this organization
    if not request.user.is_staff:
        if not organization.memberships.filter(user=request.user, is_active=True).exists():
            raise PermissionDenied("You don't have access to this organization")

    queryset = OrganizationModule.objects.filter(
        organization=organization
    ).select_related("organization", "organization__owner", "organization__created_by", "module")
    serializer = OrganizationModuleSerializer(queryset, many=True, context={"request": request})
    return Response(serializer.data)


@extend_schema(
    responses=OrganizationModuleSerializer,
    tags=["Organizations"],
    methods=["POST"]
)
@extend_schema(
    responses={"204": None},
    tags=["Organizations"],
    methods=["DELETE"]
)
@api_view(['POST', 'DELETE'])
@permission_classes([IsAuthenticated])
def organization_set_module_view(request, org_id, code):
    """
    Enable or disable a module for the organization.
    """
    try:
        organization = Organization.objects.get(pk=org_id)
    except Organization.DoesNotExist:
        return Response({"detail": "Organization not found."}, status=status.HTTP_404_NOT_FOUND)

    # Check if user is admin of the organization
    if not (request.user.is_staff or
            organization.memberships.filter(
                user=request.user,
                role='ADMIN',
                is_active=True
            ).exists()):
        raise PermissionDenied("You don't have permission to manage modules for this organization")

    try:
        module = Module.objects.get(code=code)
    except Module.DoesNotExist:
        return Response({"detail": "Module not found."}, status=status.HTTP_404_NOT_FOUND)

    if request.method == "DELETE":
        OrganizationModule.objects.filter(
            organization=organization, module=module
        ).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    obj, _ = OrganizationModule.objects.get_or_create(
        organization=organization,
        module=module,
        defaults={"is_enabled": True},
    )
    serializer = OrganizationModuleSerializer(obj, context={"request": request})
    return Response({"data": serializer.data})


@extend_schema(
    responses=ModuleSerializer(many=True),
    tags=["Organizations"],
    methods=["GET"]
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def modules_catalog_view(request):
    """
    List all available modules that can be enabled.
    """
    queryset = Module.objects.all()
    serializer = ModuleSerializer(queryset, many=True, context={"request": request})
    return Response({"data": serializer.data})


# Membership Management Views
@extend_schema(
    responses=OrgMembershipSerializer(many=True),
    tags=["Organization Memberships"],
    methods=["GET"]
)
@extend_schema(
    request=OrgMembershipSerializer,
    responses=OrgMembershipSerializer,
    tags=["Organization Memberships"],
    methods=["POST"]
)
@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def membership_list_view(request):
    """
    List or create organization memberships.
    """
    if request.method == 'GET':
        # Users can only see memberships for organizations they belong to
        if request.user.is_staff:
            memberships = OrgMembership.objects.all().select_related('user', 'organization')
        else:
            user_org_ids = request.user.memberships.filter(
                is_active=True
            ).values_list('organization_id', flat=True)
            memberships = OrgMembership.objects.filter(
                organization_id__in=user_org_ids,
                is_active=True
            ).select_related('user', 'organization')

        serializer = OrgMembershipSerializer(memberships, many=True, context={"request": request})
        return Response({"data": serializer.data})

    elif request.method == 'POST':
        serializer = OrgMembershipSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        organization = serializer.validated_data['organization']
        # Check if user is admin of the organization
        if not (request.user.is_staff or
                organization.memberships.filter(
                    user=request.user,
                    role='ADMIN',
                    is_active=True
                ).exists()):
            raise PermissionDenied("You don't have permission to add members to this organization")

        serializer.save()
        return Response({"data": serializer.data}, status=status.HTTP_201_CREATED)


@extend_schema(
    responses=OrgMembershipSerializer,
    tags=["Organization Memberships"],
    methods=["GET"]
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def membership_detail_view(request, pk):
    """
    Retrieve a specific membership.
    """
    try:
        if request.user.is_staff:
            membership = OrgMembership.objects.select_related('user', 'organization').get(pk=pk)
        else:
            user_org_ids = request.user.memberships.filter(
                is_active=True
            ).values_list('organization_id', flat=True)
            membership = OrgMembership.objects.select_related('user', 'organization').get(
                pk=pk,
                organization_id__in=user_org_ids
            )
    except OrgMembership.DoesNotExist:
        return Response({"detail": "Membership not found."}, status=status.HTTP_404_NOT_FOUND)

    serializer = OrgMembershipSerializer(membership, context={"request": request})
    return Response({"data": serializer.data})


@extend_schema(
    request=OrgMembershipSerializer,
    responses=OrgMembershipSerializer,
    tags=["Organization Memberships"],
    methods=["PUT", "PATCH"]
)
@api_view(['PUT', 'PATCH'])
@permission_classes([IsAuthenticated])
def membership_update_view(request, pk):
    """
    Update a membership.
    """
    try:
        membership = OrgMembership.objects.get(pk=pk)
    except OrgMembership.DoesNotExist:
        return Response({"detail": "Membership not found."}, status=status.HTTP_404_NOT_FOUND)

    # Check if user is admin of the organization
    if not (request.user.is_staff or
            membership.organization.memberships.filter(
                user=request.user,
                role='ADMIN',
                is_active=True
            ).exists()):
        raise PermissionDenied("You don't have permission to update this membership")

    partial = request.method == 'PATCH'
    serializer = OrgMembershipSerializer(
        membership,
        data=request.data,
        partial=partial,
        context={"request": request}
    )
    serializer.is_valid(raise_exception=True)
    serializer.save()
    return Response({"data": serializer.data})


@extend_schema(
    responses={"204": None},
    tags=["Organization Memberships"],
    methods=["DELETE"]
)
@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def membership_delete_view(request, pk):
    """
    Delete (deactivate) a membership.
    """
    try:
        membership = OrgMembership.objects.get(pk=pk)
    except OrgMembership.DoesNotExist:
        return Response({"detail": "Membership not found."}, status=status.HTTP_404_NOT_FOUND)

    # Check if user is admin of the organization
    if not (request.user.is_staff or
            membership.organization.memberships.filter(
                user=request.user,
                role='ADMIN',
                is_active=True
            ).exists()):
        raise PermissionDenied("You don't have permission to delete this membership")

    # Prevent removing the organization owner
    if membership.user == membership.organization.owner:
        raise ValidationError("Cannot remove organization owner")

    membership.is_active = False
    membership.save(update_fields=['is_active'])
    return Response(status=status.HTTP_204_NO_CONTENT)


# Onboarding Flow Endpoints
@extend_schema(
    request=OrganizationSerializer,
    responses=OrganizationSerializer,
    tags=["Organization Onboarding"],
    methods=["POST"]
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def organization_onboarding_create_view(request):
    """
    Create a new organization with the requesting user as admin member.
    This is part of the onboarding flow.
    """
    # Set the requesting user as both owner and creator
    serializer = OrganizationSerializer(
        data={**request.data, "owner_email": request.user.email},
        context={"request": request}
    )
    serializer.is_valid(raise_exception=True)

    # Create the organization
    organization = serializer.save(
        created_by=request.user,
        owner=request.user
    )

    # Automatically create admin membership for the requesting user
    OrgMembership.objects.create(
        organization=organization,
        user=request.user,
        role=OrgMembership.ADMIN,
        is_active=True
    )

    return Response({"data": serializer.data}, status=status.HTTP_201_CREATED)


@extend_schema(
    request=OrganizationModuleSerializer,
    responses=OrganizationModuleSerializer,
    tags=["Organization Onboarding"],
    methods=["POST"]
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def organization_onboarding_enable_module_view(request, org_id):
    """
    Enable a module for an organization during onboarding.
    Only organization admins can enable modules.
    """
    try:
        organization = Organization.objects.get(pk=org_id)
    except Organization.DoesNotExist:
        return Response({"detail": "Organization not found."}, status=status.HTTP_404_NOT_FOUND)

    # Check if user is admin of the organization
    if not (request.user.is_staff or
            organization.memberships.filter(
                user=request.user,
                role='ADMIN',
                is_active=True
            ).exists()):
        raise PermissionDenied("You don't have permission to enable modules for this organization")

    # Get the module code from request data
    module_code = request.data.get('module')
    if not module_code:
        return Response({"detail": "Module code is required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        module = Module.objects.get(code=module_code)
    except Module.DoesNotExist:
        return Response({"detail": "Module not found."}, status=status.HTTP_404_NOT_FOUND)

    # Create or update organization module
    org_module, created = OrganizationModule.objects.get_or_create(
        organization=organization,
        module=module,
        defaults={'is_active': True}
    )

    if not created and not org_module.is_active:
        org_module.is_active = True
        org_module.save(update_fields=['is_active'])

    serializer = OrganizationModuleSerializer(org_module, context={"request": request})
    return Response({"data": serializer.data}, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

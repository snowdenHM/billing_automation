# apps/organizations/views/api_keys.py
"""
Organization API-key management views (function-based).
"""
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied
from rest_framework_api_key.models import APIKey
from drf_spectacular.utils import extend_schema

from apps.organizations.models import Organization, OrganizationAPIKey
from apps.organizations.serializers import APIKeyIssueSerializer, APIKeySerializer


@extend_schema(
    request=APIKeyIssueSerializer,
    responses=APIKeySerializer,
    tags=["Organizations"],
    methods=["POST"],
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def organization_issue_api_key_view(request, org_id):
    """Issue a new API key for the organization."""
    try:
        organization = Organization.objects.get(pk=org_id)
    except Organization.DoesNotExist:
        return Response({"detail": "Organization not found."}, status=status.HTTP_404_NOT_FOUND)

    if not (
        request.user.is_staff
        or organization.memberships.filter(user=request.user, role="ADMIN", is_active=True).exists()
    ):
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
                "key": key,
            }
        },
        status=status.HTTP_201_CREATED,
    )


@extend_schema(
    responses=APIKeySerializer(many=True),
    tags=["Organizations"],
    methods=["GET"],
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def organization_list_api_keys_view(request, org_id):
    """List API keys for the organization."""
    try:
        organization = Organization.objects.get(pk=org_id)
    except Organization.DoesNotExist:
        return Response({"detail": "Organization not found."}, status=status.HTTP_404_NOT_FOUND)

    if not (
        request.user.is_staff
        or organization.memberships.filter(user=request.user, role="ADMIN", is_active=True).exists()
    ):
        raise PermissionDenied("You don't have permission to view API keys for this organization")

    queryset = OrganizationAPIKey.objects.filter(organization=organization).select_related(
        "created_by", "organization", "organization__owner", "organization__created_by"
    )
    serializer = APIKeySerializer(queryset, many=True, context={"request": request})
    return Response(serializer.data)


@extend_schema(
    responses=APIKeySerializer,
    tags=["Organizations"],
    methods=["POST"],
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def organization_revoke_api_key_view(request, org_id, key_id):
    """Revoke an API key for the organization."""
    try:
        organization = Organization.objects.get(pk=org_id)
    except Organization.DoesNotExist:
        return Response({"detail": "Organization not found."}, status=status.HTTP_404_NOT_FOUND)

    if not (
        request.user.is_staff
        or organization.memberships.filter(user=request.user, role="ADMIN", is_active=True).exists()
    ):
        raise PermissionDenied("You don't have permission to revoke API keys for this organization")

    try:
        api_key = OrganizationAPIKey.objects.get(organization=organization, id=key_id)
    except OrganizationAPIKey.DoesNotExist:
        return Response({"detail": "API key not found."}, status=status.HTTP_404_NOT_FOUND)

    api_key.api_key.revoked = True
    api_key.api_key.save()
    return Response({"data": APIKeySerializer(api_key, context={"request": request}).data})

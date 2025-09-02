# apps/organizations/class_views.py

from rest_framework import status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.generics import GenericAPIView
from rest_framework.exceptions import PermissionDenied
from drf_spectacular.utils import extend_schema

from .models import (
    Organization,
    Module,
    OrganizationModule,
    OrganizationAPIKey,
)
from .serializers import (
    APIKeySerializer,
    APIKeyRevokeResponseSerializer,
    OrganizationModuleSerializer,
    ModuleToggleSerializer,
)


class OrganizationRevokeAPIKeyView(GenericAPIView):
    """
    Revoke an API key for the organization.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = APIKeyRevokeResponseSerializer

    @extend_schema(
        responses={200: APIKeyRevokeResponseSerializer},
        tags=["Organizations"],
    )
    def post(self, request, org_id, key_id):
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
        return Response(APIKeySerializer(api_key, context={"request": request}).data)


class OrganizationSetModuleView(GenericAPIView):
    """
    Enable or disable a module for the organization.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = ModuleToggleSerializer

    @extend_schema(
        responses={200: OrganizationModuleSerializer},
        tags=["Organizations"],
    )
    def post(self, request, org_id, code):
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

        obj, _ = OrganizationModule.objects.get_or_create(
            organization=organization,
            module=module,
            defaults={"is_enabled": True},
        )
        serializer = OrganizationModuleSerializer(obj, context={"request": request})
        return Response(serializer.data)

    @extend_schema(
        responses={204: None},
        tags=["Organizations"],
    )
    def delete(self, request, org_id, code):
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

        OrganizationModule.objects.filter(
            organization=organization, module=module
        ).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

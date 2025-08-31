from django.shortcuts import get_object_or_404
from django.contrib.auth import get_user_model
from rest_framework import status, viewsets
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.decorators import action
from drf_spectacular.utils import extend_schema, OpenApiParameter
from drf_spectacular.types import OpenApiTypes
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
    APIKeyRevokeSerializer,
    ModuleSerializer,
    OrganizationModuleSerializer,
)
from .permissions import IsOrgAdminForObject
from apps.common.permissions import IsSuperAdmin

User = get_user_model()


@extend_schema(tags=["Organizations"])
class OrganizationViewSet(viewsets.ModelViewSet):
    serializer_class = OrganizationSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Organization.objects.all().select_related(
            "owner",
            "created_by"
        )

    def get_permissions(self):
        if self.action in ["create", "destroy"]:
            return [IsSuperAdmin()]
        if self.action in [
            "update",
            "partial_update",
            "add_member",
            "issue_api_key",
            "list_api_keys",
            "revoke_api_key",
            "modules",
            "set_module",
            "catalog_modules",
        ]:
            return [IsOrgAdminForObject()]
        return super().get_permissions()

    @extend_schema(
        request=OrgMembershipSerializer,
        responses=OrgMembershipSerializer,
    )
    @action(detail=True, methods=["post"])
    def add_member(self, request, pk=None):
        organization = self.get_object()
        serializer = OrgMembershipSerializer(
            data={**request.data, "organization": organization.id},
            context=self.get_serializer_context(),
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True)
    def members(self, request, pk=None):
        organization = self.get_object()
        queryset = OrgMembership.objects.filter(
            organization=organization
        ).select_related("user", "organization")
        serializer = OrgMembershipSerializer(queryset, many=True)
        return Response(serializer.data)

    @extend_schema(
        request=APIKeyIssueSerializer,
        responses=APIKeySerializer,
    )
    @action(detail=True, methods=["post"])
    def issue_api_key(self, request, pk=None):
        organization = self.get_object()
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
                **APIKeySerializer(org_api_key).data,
                "key": key,  # Include the actual key in response
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True)
    def list_api_keys(self, request, pk=None):
        organization = self.get_object()
        queryset = OrganizationAPIKey.objects.filter(
            organization=organization
        ).select_related("created_by", "organization", "organization__owner", "organization__created_by")
        serializer = APIKeySerializer(queryset, many=True)
        return Response(serializer.data)

    @extend_schema(
        request=APIKeyRevokeSerializer,
        responses=APIKeySerializer,
        parameters=[
            OpenApiParameter(
                name="key_id",
                location=OpenApiParameter.PATH,
                type=OpenApiTypes.UUID,
                description="The UUID of the API key to revoke"
            )
        ]
    )
    @action(detail=True, methods=["post"], url_path="api-keys/(?P<key_id>[^/.]+)/revoke")
    def revoke_api_key(self, request, pk=None, key_id=None):
        organization = self.get_object()
        api_key = get_object_or_404(
            OrganizationAPIKey, organization=organization, id=key_id
        )
        api_key.api_key.revoked = True
        api_key.api_key.save()
        return Response(APIKeySerializer(api_key).data)

    @action(detail=True)
    def modules(self, request, pk=None):
        organization = self.get_object()
        queryset = OrganizationModule.objects.filter(
            organization=organization
        ).select_related("organization", "organization__owner", "organization__created_by", "module")
        serializer = OrganizationModuleSerializer(queryset, many=True)
        return Response(serializer.data)

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="code",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.PATH,
                description="Module code",
            )
        ],
    )
    @action(
        detail=True,
        methods=["post", "delete"],
        url_path="modules/(?P<code>[^/.]+)",
    )
    def set_module(self, request, pk=None, code=None):
        organization = self.get_object()
        module = get_object_or_404(Module, code=code)

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
        serializer = OrganizationModuleSerializer(obj)
        return Response(serializer.data)

    @action(detail=False, url_path="modules/catalog")
    def catalog_modules(self, request):
        """List all available modules that can be enabled."""
        queryset = Module.objects.all()
        serializer = ModuleSerializer(queryset, many=True)
        return Response(serializer.data)

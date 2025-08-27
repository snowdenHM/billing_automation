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
    queryset = Organization.objects.all().select_related("owner", "created_by")
    serializer_class = OrganizationSerializer
    permission_classes = [IsAuthenticated]

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
            return [IsAuthenticated(), IsOrgAdminForObject()]
        return super().get_permissions()

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    # ---------- Members ----------

    @extend_schema(summary="List organization members", tags=["Organization Members"], responses=OrgMembershipSerializer(many=True))
    @action(detail=True, methods=["get"], url_path="members")
    def members(self, request, pk=None):
        org = self.get_object()
        qs = OrgMembership.objects.filter(organization=org).select_related("user")
        ser = OrgMembershipSerializer(qs, many=True)
        return Response(ser.data)

    @extend_schema(summary="Add/update organization members", tags=["Organization Members"], request=OrgMembershipSerializer, responses=OrgMembershipSerializer)
    @members.mapping.post
    def add_member(self, request, pk=None):
        org = self.get_object()
        self.check_object_permissions(request, org)
        data = request.data.copy()
        data["organization"] = org.id
        ser = OrgMembershipSerializer(data=data)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(ser.data, status=status.HTTP_201_CREATED)

    # ---------- API Keys ----------

    @extend_schema(summary="Issue an API key for this organization", tags=["Organization API Keys"], request=APIKeyIssueSerializer, responses=APIKeySerializer)
    @action(detail=True, methods=["post"], url_path="apikeys/issue")
    def issue_api_key(self, request, pk=None):
        org = self.get_object()
        self.check_object_permissions(request, org)
        ser = APIKeyIssueSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        name = ser.validated_data["name"]
        api_key_obj, key = APIKey.objects.create_key(name=name)
        link = OrganizationAPIKey.objects.create(
            api_key=api_key_obj,
            organization=org,
            name=name,
            created_by=request.user,
        )
        return Response(
            {
                "id": link.id,
                "name": link.name,
                "organization": org.id,
                "api_key": key,
            },
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(summary="List API keys for this organization", tags=["Organization API Keys"])
    @action(detail=True, methods=["get"], url_path="apikeys")
    def list_api_keys(self, request, pk=None):
        org = self.get_object()
        self.check_object_permissions(request, org)
        qs = OrganizationAPIKey.objects.filter(organization=org).select_related("api_key")
        data = [{"id": link.id, "name": link.name, "revoked": link.api_key.revoked, "created_at": link.created_at} for link in qs]
        return Response(data)

    @extend_schema(
        summary="Revoke an API key for this organization",
        tags=["Organization API Keys"],
        parameters=[OpenApiParameter(name="key_id", type=OpenApiTypes.UUID, location=OpenApiParameter.PATH)],
        responses={"200": None},
    )
    @action(detail=True, methods=["post"], url_path=r"apikeys/(?P<key_id>[0-9a-f-]{36})/revoke")
    def revoke_api_key(self, request, key_id=None, pk=None):
        org = self.get_object()
        self.check_object_permissions(request, org)
        link = get_object_or_404(OrganizationAPIKey, id=key_id, organization=org)
        ser = APIKeyRevokeSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        link.api_key.revoked = True
        link.api_key.save(update_fields=["revoked"])
        return Response({"detail": "API key revoked."})

    # ---------- Modules Catalog & Entitlements ----------

    @extend_schema(summary="List catalog modules (admin-defined)", tags=["Organization Modules"], responses=ModuleSerializer(many=True))
    @action(detail=False, methods=["get"], url_path="modules/catalog")
    def catalog_modules(self, request):
        modules = Module.objects.all().order_by("code")
        return Response(ModuleSerializer(modules, many=True).data)

    @extend_schema(summary="List enabled modules for this organization", tags=["Organization Modules"], responses=OrganizationModuleSerializer(many=True))
    @action(detail=True, methods=["get"], url_path="modules")
    def modules(self, request, pk=None):
        org = self.get_object()
        self.check_object_permissions(request, org)
        qs = OrganizationModule.objects.filter(organization=org).select_related("module")
        return Response(OrganizationModuleSerializer(qs, many=True).data)

    @extend_schema(summary="Enable/disable a module for this organization", tags=["Organization Modules"], request=OrganizationModuleSerializer, responses=OrganizationModuleSerializer)
    @modules.mapping.post
    def set_module(self, request, pk=None):
        org = self.get_object()
        self.check_object_permissions(request, org)
        data = request.data.copy()
        data["organization"] = org.id
        ser = OrganizationModuleSerializer(data=data)
        ser.is_valid(raise_exception=True)
        obj = ser.save()
        return Response(OrganizationModuleSerializer(obj).data)

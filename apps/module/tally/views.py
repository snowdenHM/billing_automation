import re
from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, BasePermission
from rest_framework_api_key.permissions import HasAPIKey
from django.db import transaction
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, OpenApiParameter
from drf_spectacular.openapi import OpenApiTypes

from apps.organizations.models import Organization
from apps.common.permissions import IsOrgAdmin

from apps.module.tally.models import Ledger, ParentLedger, TallyConfig
from apps.module.tally.serializers import (
    LedgerSerializer,
    TallyConfigSerializer,
    LedgerBulkCreateSerializer
)
from .vendor_views import TallyVendorBillViewSet


class OrganizationAPIKeyOrBearerToken(BasePermission):
    """
    Custom permission class that allows access via API key OR Bearer token authentication.
    This is an OR condition between authentication methods.
    """
    def has_permission(self, request, view):
        # Check for API key in the Authorization header
        auth_header = request.META.get('HTTP_AUTHORIZATION', '')

        if auth_header.startswith('Api-Key '):
            api_key_value = auth_header.replace('Api-Key ', '', 1)

            # Check if the API key exists and is valid
            from rest_framework_api_key.models import APIKey
            from apps.organizations.models import OrganizationAPIKey

            try:
                # First check if the raw API key is valid
                api_key_obj = APIKey.objects.get_from_key(api_key_value)

                # Check if the API key is valid with the actual key string
                if api_key_obj and api_key_obj.is_valid(api_key_value):
                    # Then check if it's linked to an organization
                    org_api_key = OrganizationAPIKey.objects.get(api_key=api_key_obj)

                    # Store the organization in the request for later use
                    request.organization = org_api_key.organization
                    return True
            except Exception:
                # Any exception means the API key is invalid or doesn't exist
                pass

        # If not authenticated via API key, check for Bearer token
        bearer_auth = IsAuthenticated().has_permission(request, view)
        if bearer_auth:
            # If authenticated via bearer token, also check admin permission
            return IsOrgAdmin().has_permission(request, view)

        return False


@extend_schema(tags=['Tally Config'])
class TallyConfigViewSet(viewsets.ModelViewSet):
    serializer_class = TallyConfigSerializer
    permission_classes = [OrganizationAPIKeyOrBearerToken]

    def get_queryset(self):
        """Filter queryset based on organization UUID"""
        organization = self.get_organization()
        # Add explicit ordering to prevent pagination warnings
        return TallyConfig.objects.filter(organization=organization).order_by('-id')

    def get_organization(self):
        """Get organization from URL UUID parameter or API key"""
        # Extract organization UUID from URL
        org_id = self.kwargs.get('org_id')
        if org_id:
            return get_object_or_404(Organization, id=org_id)

        # If using API key, get organization from API key
        if hasattr(self.request, 'auth') and self.request.auth:
            from apps.organizations.models import OrganizationAPIKey
            try:
                org_api_key = OrganizationAPIKey.objects.get(api_key=self.request.auth)
                return org_api_key.organization
            except OrganizationAPIKey.DoesNotExist:
                pass

        # Fallback to user's first organization
        if hasattr(self.request.user, 'memberships'):
            membership = self.request.user.memberships.first()
            if membership:
                return membership.organization

        return None

    def perform_create(self, serializer):
        """Set organization when creating TallyConfig"""
        organization = self.get_organization()
        serializer.save(organization=organization)

    @extend_schema(
        summary="List Tally Configurations",
        description="Get all Tally configurations for the organization",
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(
        summary="Create Tally Configuration",
        description="Create a new Tally configuration for mapping parent ledgers to different GST types and categories",
    )
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    @extend_schema(
        summary="Retrieve Tally Configuration",
        description="Get a specific Tally configuration by ID",
    )
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(
        summary="Update Tally Configuration",
        description="Update a Tally configuration completely",
    )
    def update(self, request, *args, **kwargs):
        return super().update(request, *args, **kwargs)

    @extend_schema(
        summary="Partially Update Tally Configuration",
        description="Partially update a Tally configuration",
    )
    def partial_update(self, request, *args, **kwargs):
        return super().partial_update(request, *args, **kwargs)

    @extend_schema(
        summary="Delete Tally Configuration",
        description="Delete a Tally configuration",
    )
    def destroy(self, request, *args, **kwargs):
        return super().destroy(request, *args, **kwargs)


@extend_schema(tags=['Tally TCP'])
class LedgerViewSet(viewsets.GenericViewSet):
    """
    Simplified Ledger ViewSet with only GET and POST operations
    """
    serializer_class = LedgerSerializer
    permission_classes = [OrganizationAPIKeyOrBearerToken]

    def get_queryset(self):
        """Filter queryset based on organization UUID"""
        organization = self.get_organization()
        return Ledger.objects.filter(organization=organization).select_related('parent')

    def get_organization(self):
        """Get organization from URL UUID parameter or API key"""
        # Extract organization UUID from URL
        org_id = self.kwargs.get('org_id')
        if org_id:
            return get_object_or_404(Organization, id=org_id)

        # If using API key, get organization from API key
        if hasattr(self.request, 'auth') and self.request.auth:
            from apps.organizations.models import OrganizationAPIKey
            try:
                org_api_key = OrganizationAPIKey.objects.get(api_key=self.request.auth)
                return org_api_key.organization
            except OrganizationAPIKey.DoesNotExist:
                pass

        # Fallback to user's first organization
        if hasattr(self.request.user, 'memberships'):
            membership = self.request.user.memberships.first()
            if membership:
                return membership.organization

        return None

    @extend_schema(
        summary="List Ledgers",
        description="Get all ledgers for the organization",
        responses={200: LedgerSerializer(many=True)},
    )
    def list(self, request, *args, **kwargs):
        """List all ledgers for the organization"""
        queryset = self.get_queryset()
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @extend_schema(
        summary="Bulk Create Ledgers from Tally",
        description="Create multiple ledgers from Tally data format. Expects LEDGER array with Master_Id, Name, Parent, etc.",
        request=LedgerBulkCreateSerializer,
        responses={201: LedgerSerializer(many=True)},
    )
    def create(self, request, *args, **kwargs):
        """
        Handle bulk creation of ledgers from Tally data format.
        Expects: {"LEDGER": [{"Master_Id": "...", "Name": "...", ...}, ...]}
        """
        # Print the full request URL for debugging
        full_url = request.build_absolute_uri()
        print(f"Full Request URL: {full_url}")

        organization = self.get_organization()
        if not organization:
            return Response(
                {'error': 'Organization not found'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Validate input data format
        bulk_serializer = LedgerBulkCreateSerializer(data=request.data)
        if not bulk_serializer.is_valid():
            return Response(bulk_serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        ledger_data = bulk_serializer.validated_data.get("LEDGER", [])

        if not ledger_data:
            return Response(
                {'message': 'No Ledger Data Provided'},
                status=status.HTTP_400_BAD_REQUEST
            )

        ledger_instances = []
        response_data = []

        try:
            with transaction.atomic():
                for ledger_entry in ledger_data:
                    parent_name = ledger_entry.get('Parent', '').strip()

                    # Fetch or create ParentLedger
                    parent_ledger, _ = ParentLedger.objects.get_or_create(
                        parent=parent_name,
                        organization=organization
                    )

                    # Prepare Ledger instance
                    ledger_instance = Ledger(
                        master_id=ledger_entry.get('Master_Id'),
                        alter_id=ledger_entry.get('Alter_Id'),
                        name=ledger_entry.get('Name'),
                        parent=parent_ledger,
                        alias=ledger_entry.get('ALIAS'),
                        opening_balance=ledger_entry.get('OpeningBalance', '0'),
                        gst_in=ledger_entry.get('GSTIN'),
                        company=ledger_entry.get('Company'),
                        organization=organization
                    )
                    ledger_instances.append(ledger_instance)

                # Bulk create ledgers for performance
                created_ledgers = Ledger.objects.bulk_create(ledger_instances)

                # Prepare response data
                for ledger_instance in created_ledgers:
                    response_data.append({
                        'id': str(ledger_instance.id),
                        'master_id': ledger_instance.master_id,
                        'alter_id': ledger_instance.alter_id,
                        'name': ledger_instance.name,
                        'parent': ledger_instance.parent.parent,
                        'alias': ledger_instance.alias,
                        'opening_balance': str(ledger_instance.opening_balance),
                        'gst_in': ledger_instance.gst_in,
                        'company': ledger_instance.company
                    })

            return Response(response_data, status=status.HTTP_201_CREATED)

        except Exception as e:
            print(f"Error creating ledgers: {str(e)}")
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

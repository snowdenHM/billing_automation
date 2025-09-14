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
                # Check if the API key is valid
                api_key_obj = APIKey.objects.get_from_key(api_key_value)

                if api_key_obj:
                    # Check if it's linked to an organization
                    org_api_key = OrganizationAPIKey.objects.get(api_key=api_key_obj)

                    # Store the organization in the request for later use
                    request.organization = org_api_key.organization
                    return True

            except (APIKey.DoesNotExist, OrganizationAPIKey.DoesNotExist):
                # API key doesn't exist or not linked to organization
                pass
            except Exception as e:
                # Log other exceptions for debugging
                print(f"API Key validation error: {str(e)}")
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
        """Filter queryset based on organization UUID with proper prefetching"""
        organization = self.get_organization()
        # Add explicit ordering and prefetch related parent ledgers for better performance
        return TallyConfig.objects.filter(organization=organization).prefetch_related(
            'igst_parents',
            'cgst_parents',
            'sgst_parents',
            'vendor_parents',
            'chart_of_accounts_parents',
            'chart_of_accounts_expense_parents'
        ).order_by('-id')

    def get_organization(self):
        """Get organization from URL UUID parameter or API key"""
        # Extract organization UUID from URL
        org_id = self.kwargs.get('org_id')
        if org_id:
            return get_object_or_404(Organization, id=org_id)

        # If using API key, get organization from request (set by permission class)
        if hasattr(self.request, 'organization'):
            return self.request.organization

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

    def dispatch(self, request, *args, **kwargs):
        """Intercept all incoming calls for logging and debugging"""
        # Log the incoming request
        print(f"TallyConfigViewSet - {request.method} {request.get_full_path()}")
        print(f"Request Headers: {dict(request.headers)}")
        print(f"Request Data: {request.data if hasattr(request, 'data') else 'No data'}")

        # Get organization info for debugging
        try:
            org = self.get_organization()
            print(f"Organization: {org.name if org else 'None'} (ID: {org.id if org else 'None'})")
        except Exception as e:
            print(f"Error getting organization: {str(e)}")

        return super().dispatch(request, *args, **kwargs)

    @extend_schema(
        summary="List Tally Configurations",
        description="Get all Tally configurations for the organization",
    )
    def list(self, request, *args, **kwargs):
        print(f"TallyConfigViewSet.list called for org: {self.get_organization()}")
        return super().list(request, *args, **kwargs)

    @extend_schema(
        summary="Create Tally Configuration",
        description="Create a new Tally configuration for mapping parent ledgers to different GST types and categories. Accepts bulk data ingestion.",
    )
    def create(self, request, *args, **kwargs):
        print(f"TallyConfigViewSet.create called with data: {request.data}")
        organization = self.get_organization()
        print(f"Creating TallyConfig for organization: {organization}")

        # Handle bulk creation if data is a list
        if isinstance(request.data, list):
            created_configs = []
            errors = []

            for config_data in request.data:
                try:
                    serializer = self.get_serializer(data=config_data)
                    if serializer.is_valid():
                        config = serializer.save(organization=organization)
                        created_configs.append(serializer.data)
                        print(f"Created config: {config.id}")
                    else:
                        errors.append({
                            'data': config_data,
                            'errors': serializer.errors
                        })
                        print(f"Validation errors for config: {serializer.errors}")
                except Exception as e:
                    errors.append({
                        'data': config_data,
                        'error': str(e)
                    })
                    print(f"Error creating config: {str(e)}")

            response_data = {
                'created': created_configs,
                'errors': errors,
                'created_count': len(created_configs),
                'error_count': len(errors)
            }

            if errors:
                return Response(response_data, status=status.HTTP_207_MULTI_STATUS)
            else:
                return Response(response_data, status=status.HTTP_201_CREATED)

        # Handle single creation
        return super().create(request, *args, **kwargs)

    @extend_schema(
        summary="Retrieve Tally Configuration",
        description="Get a specific Tally configuration by ID",
    )
    def retrieve(self, request, *args, **kwargs):
        config_id = kwargs.get('pk')
        print(f"TallyConfigViewSet.retrieve called for config ID: {config_id}")
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(
        summary="Update Tally Configuration",
        description="Update a Tally configuration completely",
    )
    def update(self, request, *args, **kwargs):
        config_id = kwargs.get('pk')
        print(f"TallyConfigViewSet.update called for config ID: {config_id} with data: {request.data}")
        return super().update(request, *args, **kwargs)

    @extend_schema(
        summary="Partially Update Tally Configuration",
        description="Partially update a Tally configuration",
    )
    def partial_update(self, request, *args, **kwargs):
        config_id = kwargs.get('pk')
        print(f"TallyConfigViewSet.partial_update called for config ID: {config_id} with data: {request.data}")
        return super().partial_update(request, *args, **kwargs)

    @extend_schema(
        summary="Delete Tally Configuration",
        description="Delete a Tally configuration",
    )
    def destroy(self, request, *args, **kwargs):
        config_id = kwargs.get('pk')
        print(f"TallyConfigViewSet.destroy called for config ID: {config_id}")
        return super().destroy(request, *args, **kwargs)


@extend_schema(tags=['Tally TCP'])
class LedgerViewSet(viewsets.GenericViewSet):
    """
    Simplified Ledger ViewSet with only GET and POST operations
    OPEN FOR TESTING - NO AUTHENTICATION REQUIRED
    """
    serializer_class = LedgerSerializer
    permission_classes = []  # No authentication required for testing

    def get_queryset(self):
        """Filter queryset based on organization UUID"""
        organization = self.get_organization()
        if organization:
            return Ledger.objects.filter(organization=organization).select_related('parent')
        else:
            # If no organization found, return all ledgers for testing
            return Ledger.objects.all().select_related('parent')

    def get_organization(self):
        """Get organization from URL UUID parameter or API key"""
        # Extract organization UUID from URL
        org_id = self.kwargs.get('org_id')
        if org_id:
            try:
                return Organization.objects.get(id=org_id)
            except Organization.DoesNotExist:
                print(f"Organization with ID {org_id} not found")
                return None

        # If using API key, get organization from API key (optional for testing)
        if hasattr(self.request, 'auth') and self.request.auth:
            from apps.organizations.models import OrganizationAPIKey
            try:
                org_api_key = OrganizationAPIKey.objects.get(api_key=self.request.auth)
                return org_api_key.organization
            except OrganizationAPIKey.DoesNotExist:
                pass

        # If using request.organization from permission class (optional for testing)
        if hasattr(self.request, 'organization'):
            return self.request.organization

        # Fallback to user's first organization (optional for testing)
        if hasattr(self.request.user, 'memberships') and self.request.user.is_authenticated:
            membership = self.request.user.memberships.first()
            if membership:
                return membership.organization

        print("No organization found, operating in test mode")
        return None

    def dispatch(self, request, *args, **kwargs):
        """Intercept all incoming calls for logging and debugging"""
        # Log the incoming request
        print(f"LedgerViewSet - {request.method} {request.get_full_path()}")
        print(f"Request Headers: {dict(request.headers)}")
        print(f"Request Data: {request.data if hasattr(request, 'data') else 'No data'}")

        # Get organization info for debugging
        try:
            org = self.get_organization()
            print(f"Organization: {org.name if org else 'TEST MODE - No Org'} (ID: {org.id if org else 'None'})")
        except Exception as e:
            print(f"Error getting organization: {str(e)}")

        return super().dispatch(request, *args, **kwargs)

    @extend_schema(
        summary="List Ledgers",
        description="Get all ledgers for the organization grouped by parent ledger (No authentication required for testing)",
        responses={200: LedgerSerializer(many=True)},
    )
    def list(self, request, *args, **kwargs):
        """List all ledgers for the organization grouped by parent ledger"""
        print(f"LedgerViewSet.list called - TEST MODE")
        queryset = self.get_queryset()
        print(f"Found {queryset.count()} ledgers")

        # Group ledgers by parent ledger
        grouped_ledgers = {}

        for ledger in queryset:
            parent_name = ledger.parent.parent if ledger.parent else "Uncategorized"
            parent_id = str(ledger.parent.id) if ledger.parent else "uncategorized"

            # Initialize parent group if not exists
            if parent_name not in grouped_ledgers:
                grouped_ledgers[parent_name] = {
                    "parent_id": parent_id,
                    "parent_name": parent_name,
                    "ledger_count": 0,
                    "ledgers": []
                }

            # Add ledger to parent group
            ledger_data = {
                "id": str(ledger.id),
                "master_id": ledger.master_id,
                "alter_id": ledger.alter_id,
                "name": ledger.name,
                "alias": ledger.alias,
                "opening_balance": str(ledger.opening_balance),
                "gst_in": ledger.gst_in,
                "company": ledger.company
            }

            grouped_ledgers[parent_name]["ledgers"].append(ledger_data)
            grouped_ledgers[parent_name]["ledger_count"] += 1

        # Convert to list format for consistent API response
        response_data = {
            "success": True,
            "total_parents": len(grouped_ledgers),
            "total_ledgers": queryset.count(),
            "grouped_ledgers": grouped_ledgers
        }

        print(f"Grouped into {len(grouped_ledgers)} parent categories")
        return Response(response_data)

    @extend_schema(
        summary="Bulk Create Ledgers from Tally",
        description="Create multiple ledgers from Tally data format. Expects LEDGER array with Master_Id, Name, Parent, etc. (No authentication required for testing)",
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
        print(f"LedgerViewSet.create called - TEST MODE")
        print(f"Raw request data type: {type(request.data)}")
        print(f"Raw request data keys: {list(request.data.keys()) if hasattr(request.data, 'keys') else 'No keys'}")

        organization = self.get_organization()
        if not organization:
            print("No organization found, using first available organization")
            try:
                organization = Organization.objects.first()
                if not organization:
                    print("No organizations exist, this might cause issues")
                    return Response(
                        {'error': 'No organization available for testing. Please create an organization first.'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
                print(f"Using organization: {organization.name} (ID: {organization.id})")
            except Exception as e:
                print(f"Error getting organization: {str(e)}")
                return Response(
                    {'error': 'Could not determine organization for testing'},
                    status=status.HTTP_400_BAD_REQUEST
                )

        # Handle the case where request.data might be empty or None
        if not request.data:
            return Response(
                {'error': 'No data provided'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Extract LEDGER data - handle both direct LEDGER key and nested structure
        ledger_data = None
        if isinstance(request.data, dict):
            ledger_data = request.data.get("LEDGER", [])
            print(f"Found LEDGER key with {len(ledger_data)} entries")
        elif isinstance(request.data, list):
            # If the data is directly a list, assume it's the ledger data
            ledger_data = request.data
            print(f"Data is directly a list with {len(ledger_data)} entries")
        else:
            print(f"Unexpected data format: {type(request.data)}")
            return Response(
                {'error': 'Invalid data format. Expected object with LEDGER key or array of ledgers.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not ledger_data:
            return Response(
                {'error': 'No Ledger Data Provided in LEDGER key'},
                status=status.HTTP_400_BAD_REQUEST
            )

        print(f"Processing {len(ledger_data)} ledger entries")
        created_ledgers = []
        failed_ledgers = []

        try:
            with transaction.atomic():
                for i, ledger_entry in enumerate(ledger_data):
                    try:
                        print(f"Processing ledger {i+1}: {ledger_entry.get('Name', 'Unknown')} with parent: {ledger_entry.get('Parent', 'Unknown')}")

                        parent_name = ledger_entry.get('Parent', '').strip()
                        if not parent_name:
                            parent_name = "Uncategorized"  # Default parent if empty

                        # Fetch or create ParentLedger
                        parent_ledger, created = ParentLedger.objects.get_or_create(
                            parent=parent_name,
                            organization=organization
                        )
                        if created:
                            print(f"Created new parent ledger: {parent_name}")

                        # Clean and convert opening balance
                        opening_balance_str = str(ledger_entry.get('OpeningBalance', '0')).strip()
                        opening_balance = clean_decimal_value(opening_balance_str)

                        # Create Ledger instance
                        ledger_instance = Ledger.objects.create(
                            master_id=ledger_entry.get('Master_Id', ''),
                            alter_id=ledger_entry.get('Alter_id', '') or ledger_entry.get('Alter_Id', ''),  # Handle both cases
                            name=ledger_entry.get('Name', ''),
                            parent=parent_ledger,
                            alias=ledger_entry.get('ALIAS', ''),
                            opening_balance=opening_balance,
                            gst_in=ledger_entry.get('GSTIN', ''),
                            company=ledger_entry.get('Company', ''),
                            organization=organization
                        )

                        created_ledgers.append({
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

                        print(f"Successfully created ledger: {ledger_instance.name}")

                    except Exception as ledger_error:
                        print(f"Error creating individual ledger {i+1}: {str(ledger_error)}")
                        failed_ledgers.append({
                            'index': i+1,
                            'name': ledger_entry.get('Name', 'Unknown'),
                            'error': str(ledger_error),
                            'data': ledger_entry
                        })
                        # Continue processing other ledgers instead of failing the entire transaction
                        continue

            print(f"Successfully created {len(created_ledgers)} ledgers")
            print(f"Failed to create {len(failed_ledgers)} ledgers")

            # Return detailed response
            response_data = {
                'success': True,
                'created_count': len(created_ledgers),
                'failed_count': len(failed_ledgers),
                'created_ledgers': created_ledgers,
                'failed_ledgers': failed_ledgers[:10] if failed_ledgers else []  # Limit failed entries in response
            }

            if failed_ledgers:
                return Response(response_data, status=status.HTTP_207_MULTI_STATUS)
            else:
                return Response(response_data, status=status.HTTP_201_CREATED)

        except Exception as e:
            print(f"Error in bulk creation transaction: {str(e)}")
            return Response({
                'error': f'Bulk creation failed: {str(e)}',
                'created_count': len(created_ledgers),
                'failed_count': len(failed_ledgers)
            }, status=status.HTTP_400_BAD_REQUEST)


def clean_decimal_value(value_str):
    """Clean decimal value by removing commas and converting to proper decimal format"""
    if not value_str or value_str in ['', '0', None]:
        return '0.00'

    try:
        # Remove commas and any extra whitespace
        cleaned = str(value_str).replace(',', '').strip()

        # Handle empty or non-numeric strings
        if not cleaned or cleaned == '':
            return '0.00'

        # Convert to float first to validate, then back to string for DecimalField
        float_val = float(cleaned)
        return f"{float_val:.2f}"

    except (ValueError, TypeError) as e:
        print(f"Error cleaning decimal value '{value_str}': {str(e)}")
        return '0.00'

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from django.shortcuts import get_object_or_404
from django.db import transaction
from django.utils import timezone
from django.urls import reverse
from rest_framework_api_key.models import APIKey
from drf_spectacular.utils import extend_schema
from apps.organizations.models import Organization, OrganizationAPIKey
from .models import ParentLedger, Ledger, StockItem, TallyVendorBill, TallyExpenseBill


@api_view(['GET'])
@permission_classes([IsAuthenticated])
@extend_schema(tags=['Tally Config'])
def organization_tally_data(request, org_id):
    """
    Comprehensive endpoint that returns URLs for all Tally data endpoints for an organization:
    1. Organization ledgers complete endpoint URL
    2. Organization Masters complete endpoint URL
    3. Organization Vendor bill sync_external complete endpoint URL
    4. Organization Expense bill sync_external complete endpoint URL
    5. Organization API Key (generate if not available)
    """
    try:
        # Get organization and verify access
        organization = get_object_or_404(Organization, id=org_id)

        # Check if user has access to this organization
        if not request.user.is_superuser:
            user_orgs = request.user.organizations.all()
            if organization not in user_orgs:
                return Response(
                    {"error": "You don't have access to this organization"},
                    status=status.HTTP_403_FORBIDDEN
                )

        with transaction.atomic():
            # Generate API Key if not available
            org_api_key = None
            try:
                org_api_key = OrganizationAPIKey.objects.select_related('api_key').get(
                    organization=organization
                )
                api_key_value = org_api_key.api_key.key
            except OrganizationAPIKey.DoesNotExist:
                # Generate new API key for the organization
                api_key_name = f"{organization.name} - Tally Integration"

                # Create APIKey instance
                api_key_obj, api_key_value = APIKey.objects.create_key(name=api_key_name)

                # Create OrganizationAPIKey link
                org_api_key = OrganizationAPIKey.objects.create(
                    api_key=api_key_obj,
                    organization=organization,
                    name="Tally Integration Key",
                    created_by=request.user
                )

            # Build base URL for the organization
            base_url = request.build_absolute_uri().rstrip('/').replace('/data/', '/')

            # Return URLs for each endpoint
            response_data = {
                "organization": {
                    "id": str(organization.id),
                    "name": organization.name,
                    "unique_name": organization.unique_name,
                    "status": organization.status
                },
                "ledgers": f"{base_url}ledgers/",
                "masters": f"{base_url}masters/",
                "vendor_bills_sync_external": f"{base_url}vendor-bills/sync_external/",
                "expense_bills_sync_external": f"{base_url}expense-bills/sync_external/",
                "api_key": api_key_value
            }

            return Response(response_data, status=status.HTTP_200_OK)

    except Organization.DoesNotExist:
        return Response(
            {"error": "Organization not found"},
            status=status.HTTP_404_NOT_FOUND
        )
    except Exception as e:
        return Response(
            {"error": f"An error occurred: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )

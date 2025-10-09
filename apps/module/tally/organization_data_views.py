from django.db import transaction
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, OpenApiResponse, OpenApiExample, OpenApiParameter
from rest_framework import serializers
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework_api_key.models import APIKey

from apps.organizations.models import Organization, OrganizationAPIKey


class OrganizationTallyDataResponseSerializer(serializers.Serializer):
    """Response serializer for organization tally data endpoint"""

    class OrganizationSerializer(serializers.Serializer):
        id = serializers.UUIDField(help_text="Organization UUID")
        name = serializers.CharField(help_text="Organization name")

    organization = OrganizationSerializer(help_text="Organization details")
    ledgers = serializers.URLField(help_text="URL to access organization's ledgers endpoint")
    masters = serializers.URLField(help_text="URL to access organization's masters endpoint")
    vendor_bills_sync_external = serializers.URLField(
        help_text="URL to access organization's vendor bills sync external endpoint"
    )
    expense_bills_sync_external = serializers.URLField(
        help_text="URL to access organization's expense bills sync external endpoint"
    )
    api_key = serializers.CharField(help_text="Organization's API key for external integrations")


@extend_schema(
    operation_id='get_organization_tally_data',
    tags=['Tally Config'],
    summary='Get Organization Tally Data URLs and API Key',
    description="""
    **Comprehensive endpoint for Tally integration data access**
    
    Returns URLs for all Tally data endpoints for a specific organization:
    
    • **Ledgers Endpoint** - Access all parent ledgers and ledgers for the organization
    • **Masters Endpoint** - Access all stock items and master data
    • **Vendor Bills Sync External** - Access vendor bills ready for external synchronization
    • **Expense Bills Sync External** - Access expense bills ready for external synchronization
    • **API Key** - Organization's API key (auto-generated if not exists)
    
    **Authentication**: Required  
    **Authorization**: User must have access to the specified organization
    """,
    parameters=[
        OpenApiParameter(
            name='org_id',
            type=str,
            location=OpenApiParameter.PATH,
            description='Organization UUID (e.g., 123e4567-e89b-12d3-a456-426614174000)',
            required=True
        ),
    ],
    responses={
        200: OpenApiResponse(
            response=OrganizationTallyDataResponseSerializer,
            description='Successfully retrieved organization tally data URLs and API key',
            examples=[
                OpenApiExample(
                    'Success Response',
                    summary='Successful response with all endpoint URLs',
                    description='Returns organization details, endpoint URLs, and API key',
                    value={
                        "organization": {
                            "id": "123e4567-e89b-12d3-a456-426614174000",
                            "name": "Sample Organization"
                        },
                        "ledgers": "https://api.example.com/api/v1/tally/org/123e4567-e89b-12d3-a456-426614174000/ledgers/",
                        "masters": "https://api.example.com/api/v1/tally/org/123e4567-e89b-12d3-a456-426614174000/masters/",
                        "vendor_bills_sync_external": "https://api.example.com/api/v1/tally/org/123e4567-e89b-12d3-a456-426614174000/vendor-bills/sync_external/",
                        "expense_bills_sync_external": "https://api.example.com/api/v1/tally/org/123e4567-e89b-12d3-a456-426614174000/expense-bills/sync_external/",
                        "api_key": "Authorization:Api-Key abc123def456ghi789jkl012mno345pqr678stu901vwx234yz"
                    }
                )
            ]
        ),
        403: OpenApiResponse(
            description='Forbidden - User does not have access to this organization',
            examples=[
                OpenApiExample(
                    'Access Denied',
                    summary='User lacks organization access',
                    value={"error": "You don't have access to this organization"}
                )
            ]
        ),
        404: OpenApiResponse(
            description='Organization not found',
            examples=[
                OpenApiExample(
                    'Organization Not Found',
                    summary='Invalid organization UUID',
                    value={"error": "Organization not found"}
                )
            ]
        ),
        500: OpenApiResponse(
            description='Internal server error',
            examples=[
                OpenApiExample(
                    'Server Error',
                    summary='Unexpected server error',
                    value={"error": "An error occurred: <error_message>"}
                )
            ]
        )
    }
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
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
            # Fix: Use the correct relationship through memberships
            user_orgs = Organization.objects.filter(
                memberships__user=request.user,
                memberships__is_active=True
            )
            if organization not in user_orgs:
                return Response(
                    {"error": "You don't have access to this organization"},
                    status=status.HTTP_403_FORBIDDEN
                )

        with transaction.atomic():
            # Get or create API Key - ensure only one API key per organization
            try:
                # Try to get existing API key for this organization
                org_api_key = OrganizationAPIKey.objects.select_related('api_key').get(
                    organization=organization
                )

                # Use the stored API key value
                api_key_value = org_api_key.api_key_value_gen

                # Handle existing records that don't have api_key_value_gen populated
                if not api_key_value:
                    # For existing records, use the API key ID as fallback
                    api_key_value = org_api_key.api_key.id
                    # Update the record with the fallback value
                    org_api_key.api_key_value_gen = api_key_value
                    org_api_key.save(update_fields=['api_key_value_gen'])

            except OrganizationAPIKey.DoesNotExist:
                # Use select_for_update to prevent race conditions
                organization_locked = Organization.objects.select_for_update().get(id=org_id)

                # Double-check if API key was created by another concurrent request
                existing_org_api_key = OrganizationAPIKey.objects.select_related('api_key').filter(
                    organization=organization_locked
                ).first()

                if existing_org_api_key:
                    # Another request already created the key
                    api_key_value = existing_org_api_key.api_key_value_gen
                else:
                    # Safe to create new API key
                    # Fix: Shorten the API key name to avoid database constraint issues
                    api_key_name = f"Tally-{organization.unique_name}"
                    # Ensure name doesn't exceed 50 characters (database constraint)
                    if len(api_key_name) > 50:
                        api_key_name = api_key_name[:50]

                    # Create APIKey instance - api_key_value contains the actual key string
                    api_key_obj, api_key_value = APIKey.objects.create_key(name=api_key_name)

                    try:
                        # Create OrganizationAPIKey link and store the actual key value
                        org_api_key = OrganizationAPIKey.objects.create(
                            api_key=api_key_obj,
                            api_key_value_gen=api_key_value,  # Store the actual key for future use
                            organization=organization_locked,
                            name="Tally Integration Key",
                            created_by=request.user
                        )
                    except Exception as e:
                        # If creation fails, clean up the APIKey that was created
                        api_key_obj.delete()
                        raise e

            # Build base URL for the organization
            # Get the current request URL and remove the trailing part
            current_url = request.build_absolute_uri().rstrip('/')
            # Remove '/data' from the end of the URL to get the org base URL
            if current_url.endswith('/help'):
                base_url = current_url[:-5]  # Remove '/help'
            else:
                base_url = current_url

            # Ensure base_url ends with '/'
            if not base_url.endswith('/'):
                base_url += '/'

            # Return URLs for each endpoint
            response_data = {
                "organization": {
                    "id": str(organization.id),
                    "name": organization.name
                },
                "ledgers": f"{base_url}ledgers/",
                "masters": f"{base_url}masters/",
                "vendor_bills_sync_external": f"{base_url}vendor-bills/sync_bills/",
                "expense_bills_sync_external": f"{base_url}expense-bills/sync_bills/",
                "api_key": f"Authorization:Api-Key {api_key_value}"
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

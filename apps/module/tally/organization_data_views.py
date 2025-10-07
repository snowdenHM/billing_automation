from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from django.shortcuts import get_object_or_404
from django.db import transaction
from rest_framework_api_key.models import APIKey
from drf_spectacular.utils import extend_schema, OpenApiResponse, OpenApiExample, OpenApiParameter
from rest_framework import serializers
from apps.organizations.models import Organization, OrganizationAPIKey


class OrganizationTallyDataResponseSerializer(serializers.Serializer):
    """Response serializer for organization tally data endpoint"""

    class OrganizationSerializer(serializers.Serializer):
        id = serializers.UUIDField(help_text="Organization UUID")
        name = serializers.CharField(help_text="Organization name")
        unique_name = serializers.CharField(help_text="Organization unique name (BM-XXXXX format)")
        status = serializers.CharField(help_text="Organization status")

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
                            "name": "Sample Organization",
                            "unique_name": "BM-ABC123",
                            "status": "ACTIVE"
                        },
                        "ledgers": "https://api.example.com/api/v1/tally/org/123e4567-e89b-12d3-a456-426614174000/ledgers/",
                        "masters": "https://api.example.com/api/v1/tally/org/123e4567-e89b-12d3-a456-426614174000/masters/",
                        "vendor_bills_sync_external": "https://api.example.com/api/v1/tally/org/123e4567-e89b-12d3-a456-426614174000/vendor-bills/sync_external/",
                        "expense_bills_sync_external": "https://api.example.com/api/v1/tally/org/123e4567-e89b-12d3-a456-426614174000/expense-bills/sync_external/",
                        "api_key": "abc123def456ghi789jkl012mno345pqr678stu901vwx234yz"
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
            user_orgs = request.user.organizations.all()
            if organization not in user_orgs:
                return Response(
                    {"error": "You don't have access to this organization"},
                    status=status.HTTP_403_FORBIDDEN
                )

        with transaction.atomic():
            # Generate API Key if not available
            org_api_key = None
            api_key_value = None

            try:
                org_api_key = OrganizationAPIKey.objects.select_related('api_key').get(
                    organization=organization
                )
                # For existing keys, we need to get the key that can work with get_from_key()
                # The actual key string is what's used for authentication
                api_key_value = org_api_key.api_key.id

            except OrganizationAPIKey.DoesNotExist:
                # Generate new API key for the organization
                api_key_name = f"{organization.name} - Tally Integration"

                # Create APIKey instance - api_key_value will contain the actual key string
                api_key_obj, api_key_value = APIKey.objects.create_key(name=api_key_name)

                # Create OrganizationAPIKey link
                org_api_key = OrganizationAPIKey.objects.create(
                    api_key=api_key_obj,
                    organization=organization,
                    name="Tally Integration Key",
                    created_by=request.user
                )

            # Build base URL for the organization
            base_url = request.build_absolute_uri().rstrip('/').replace('/help/', '/')

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

import re
import os
import json
from datetime import datetime
from rest_framework import viewsets, status
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated, BasePermission, AllowAny
from rest_framework_api_key.permissions import HasAPIKey
from rest_framework.pagination import PageNumberPagination
from django.db import transaction
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, OpenApiParameter
from drf_spectacular.openapi import OpenApiTypes

from apps.organizations.models import Organization
from apps.common.permissions import IsOrgAdmin

from apps.module.tally.models import Ledger, ParentLedger, TallyConfig, StockItem, TallyVendorBill, TallyExpenseBill
from apps.module.tally.vendor_views_functional import get_organization_from_request
from apps.module.tally.serializers import (
    LedgerSerializer,
    ParentLedgerSerializer,
    TallyConfigSerializer,
    LedgerBulkCreateSerializer,
    StockItemSerializer,
    StockItemBulkCreateSerializer
)


class NoPagination(PageNumberPagination):
    """Custom pagination that returns all results but maintains the paginated response format"""
    def paginate_queryset(self, queryset, request, view=None):
        # Don't actually paginate - just store the full queryset
        self.queryset = queryset
        return list(queryset)

    def get_paginated_response(self, data):
        # Return all data in the same format as paginated response but without pagination metadata
        return Response({
            'count': len(data),
            'next': None,
            'previous': None,
            'results': data
        })


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
                logger.error(f"API Key validation error: {str(e)}")
                pass

        # If not authenticated via API key, check for Bearer token
        bearer_auth = IsAuthenticated().has_permission(request, view)
        if bearer_auth:
            # If authenticated via bearer token, also check admin permission
            return IsOrgAdmin().has_permission(request, view)

        return False


@extend_schema(tags=['Tally Config'])
@api_view(['GET'])
@permission_classes([OrganizationAPIKeyOrBearerToken])  # Use same permission as ViewSet
def get_tally_config(request, org_id):
    """Get tally configuration for organization"""
    logger.info(f"get_tally_config called - Method: {request.method}, org_id: {org_id}")
    
    try:
        logger.info(f"Getting TallyConfig for organization: {org_id}")
        
        # Get organization
        organization = get_object_or_404(Organization, id=org_id)
        logger.debug(f"Found organization: {organization.name}")
        
        # Get tally config for this organization
        tally_config = TallyConfig.objects.filter(organization=organization).prefetch_related(
            'igst_parents',
            'cgst_parents', 
            'sgst_parents',
            'vendor_parents',
            'chart_of_accounts_parents',
            'chart_of_accounts_expense_parents',
            'tds_parents',
            'payment_parents'
        ).first()
        
        if not tally_config:
            logger.debug("No TallyConfig found for this organization")
            return Response({
                'success': False,
                'message': 'No tally configuration found for this organization',
                'data': None
            }, status=status.HTTP_200_OK)  # Return 200 with null data instead of 404
        
        # Serialize the config
        serializer = TallyConfigSerializer(tally_config, context={'request': request, 'organization': organization})
        
        logger.info(f"Retrieved TallyConfig: {tally_config.id}")
        return Response({
            'success': True,
            'message': 'Tally configuration retrieved successfully',
            'data': serializer.data
        }, status=status.HTTP_200_OK)
        
    except Organization.DoesNotExist:
        return Response({
            'success': False,
            'message': 'Organization not found'
        }, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        logger.error(f"Error in get_tally_config: {str(e)}")
        return Response({
            'success': False,
            'message': f'Error retrieving tally configuration: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@extend_schema(tags=['Tally Config'])
@api_view(['POST'])
@permission_classes([OrganizationAPIKeyOrBearerToken])  # Use same permission as ViewSet
def create_or_update_tally_config(request, org_id):
    """Create or update tally configuration for organization"""
    logger.info(f"create_or_update_tally_config called - Method: {request.method}, org_id: {org_id}")
    
    try:
        logger.info(f"Creating/updating TallyConfig for organization: {org_id}")
        logger.info(f"Request data: {request.data}")
        
        # Get organization
        organization = get_object_or_404(Organization, id=org_id)
        logger.debug(f"Found organization: {organization.name}")
        
        # Debug the parent ledger data being sent
        parent_fields = ['igst_parents', 'cgst_parents', 'sgst_parents', 'vendor_parents', 'chart_of_accounts_parents', 'chart_of_accounts_expense_parents', 'tds_parents', 'payment_parents']

        for field in parent_fields:
            if field in request.data:
                parent_ids = request.data[field]
                logger.info(f"{field}: {parent_ids}")
                
                if parent_ids:
                    # Check if these parent ledger IDs exist for this organization
                    existing = ParentLedger.objects.filter(
                        id__in=parent_ids, 
                        organization=organization
                    ).values_list('id', 'parent')
                    
                    existing_ids = [str(pid) for pid, _ in existing]
                    missing_ids = [pid for pid in parent_ids if str(pid) not in existing_ids]
                    
                    logger.debug(f"  Found {len(existing)} matching parent ledgers: {list(existing)}")
                    if missing_ids:
                        logger.warning(f"  Missing ParentLedger IDs: {missing_ids}")
        
        # Check if config already exists for this organization
        existing_config = TallyConfig.objects.filter(organization=organization).first()
        
        # Get tally_product_allow_sync value
        tally_product_allow_sync = request.data.get('tally_product_allow_sync', False)
        logger.info(f"tally_product_allow_sync: {tally_product_allow_sync}")
        
        if existing_config:
            logger.info(f"Updating existing TallyConfig: {existing_config.id}")
            tally_config = existing_config
        else:
            logger.info("Creating new TallyConfig")
            tally_config = TallyConfig.objects.create(
                organization=organization,
                tally_product_allow_sync=tally_product_allow_sync
            )
            
        # Set tally_product_allow_sync field
        tally_config.tally_product_allow_sync = tally_product_allow_sync
        tally_config.save()
        
        # Set parent ledgers directly
        for field in parent_fields:
            if field in request.data:
                parent_ids = request.data[field] or []
                logger.info(f"Setting {field} with IDs: {parent_ids}")
                
                # Get parent ledger objects
                parent_ledgers = ParentLedger.objects.filter(
                    id__in=parent_ids,
                    organization=organization
                )
                
                # Set the many-to-many relationship
                getattr(tally_config, field).set(parent_ledgers)
                logger.info(f"Set {field} with {parent_ledgers.count()} parent ledgers")
        
        # Refresh from database
        tally_config.refresh_from_db()
        
        # Prepare response using serializer
        context = {'request': request, 'organization': organization}
        response_serializer = TallyConfigSerializer(tally_config, context=context)
        
        message = 'Tally configuration updated successfully' if existing_config else 'Tally configuration created successfully'
        logger.info(f"Success: {message}")
        
        return Response({
            'success': True,
            'message': message,
            'data': response_serializer.data
        }, status=status.HTTP_201_CREATED if not existing_config else status.HTTP_200_OK)
            
    except Organization.DoesNotExist:
        return Response({
            'success': False,
            'message': 'Organization not found'
        }, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        logger.error(f"Error in create_or_update_tally_config: {str(e)}")
        import traceback
        traceback.print_exc()
        return Response({
            'success': False,
            'message': f'Error saving tally configuration: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@extend_schema(tags=['Tally Config ViewSet'])
class TallyConfigViewSet(viewsets.ModelViewSet):
    """ViewSet for TallyConfig with ledgers action"""
    serializer_class = TallyConfigSerializer
    permission_classes = [OrganizationAPIKeyOrBearerToken]

    def get_queryset(self):
        """Filter queryset based on organization UUID with proper prefetching"""
        organization = self.get_organization()
        return TallyConfig.objects.filter(organization=organization).prefetch_related(
            'igst_parents',
            'cgst_parents',
            'sgst_parents',
            'vendor_parents',
            'chart_of_accounts_parents',
            'chart_of_accounts_expense_parents',
            'tds_parents',
            'payment_parents'
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

    @extend_schema(
        summary="Get Ledgers by Parent Type",
        description="Get all ledgers for a specific parent type from TallyConfig",
        parameters=[
            OpenApiParameter(
                name='parent_type',
                description='Type of parent ledger to retrieve',
                required=True,
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY
            ),
            OpenApiParameter(
                name='config_id',
                description='Specific TallyConfig ID to get ledgers from (optional)',
                required=False,
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.QUERY
            )
        ],
        responses={200: LedgerSerializer(many=True)},
    )
    @action(detail=False, methods=['get'], url_path='ledgers')
    def get_ledgers_by_parent_type(self, request, org_id=None):
        """Get ledgers by parent type from TallyConfig"""
        parent_type = request.query_params.get('parent_type')
        config_id = request.query_params.get('config_id')

        if not parent_type:
            return Response(
                {'error': 'parent_type parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Valid parent types
        valid_parent_types = [
            'igst_parents',
            'cgst_parents',
            'sgst_parents',
            'vendor_parents',
            'chart_of_accounts_parents',
            'chart_of_accounts_expense_parents',
            'tds_parents',
            'payment_parents'
        ]

        if parent_type not in valid_parent_types:
            return Response(
                {
                    'error': f'Invalid parent_type. Must be one of: {", ".join(valid_parent_types)}'
                },
                status=status.HTTP_400_BAD_REQUEST
            )

        organization = self.get_organization()
        if not organization:
            return Response(
                {'error': 'Organization not found'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            # Get TallyConfig - either specific one or first available
            if config_id:
                tally_config = TallyConfig.objects.get(
                    id=config_id,
                    organization=organization
                )
            else:
                tally_config = TallyConfig.objects.filter(
                    organization=organization
                ).first()

            if not tally_config:
                return Response(
                    {'error': 'No TallyConfig found for this organization'},
                    status=status.HTTP_404_NOT_FOUND
                )

            # Get the parent ledgers for the specified type
            parent_ledgers = getattr(tally_config, parent_type).all()

            if not parent_ledgers.exists():
                return Response(
                    {
                        'message': f'No {parent_type} configured in TallyConfig',
                        'config_id': str(tally_config.id),
                        'parent_type': parent_type,
                        'ledgers': []
                    },
                    status=status.HTTP_200_OK
                )

            # Get all ledgers under these parent ledgers
            ledgers = Ledger.objects.filter(
                parent__in=parent_ledgers,
                organization=organization
            ).select_related('parent').order_by('parent__parent', 'name')

            # Group ledgers by parent for better organization
            grouped_ledgers = {}
            total_ledgers = 0

            for ledger in ledgers:
                parent_name = ledger.parent.parent
                parent_id = str(ledger.parent.id)

                if parent_name not in grouped_ledgers:
                    grouped_ledgers[parent_name] = {
                        'parent_id': parent_id,
                        'parent_name': parent_name,
                        'ledger_count': 0,
                        'ledgers': []
                    }

                ledger_data = {
                    'id': str(ledger.id),
                    'master_id': ledger.master_id,
                    'alter_id': ledger.alter_id,
                    'name': ledger.name,
                    'alias': ledger.alias,
                    'opening_balance': str(ledger.opening_balance),
                    'gst_in': ledger.gst_in,
                    'company': ledger.company
                }

                grouped_ledgers[parent_name]['ledgers'].append(ledger_data)
                grouped_ledgers[parent_name]['ledger_count'] += 1
                total_ledgers += 1

            response_data = {
                'success': True,
                'config_id': str(tally_config.id),
                'parent_type': parent_type,
                'total_parent_ledgers': parent_ledgers.count(),
                'total_ledgers': total_ledgers,
                'grouped_ledgers': grouped_ledgers
            }

            logger.info(f"Retrieved {total_ledgers} ledgers for {parent_type} from {parent_ledgers.count()} parent ledgers")
            return Response(response_data, status=status.HTTP_200_OK)

        except TallyConfig.DoesNotExist:
            return Response(
                {'error': 'TallyConfig not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        except Exception as e:
            logger.error(f"Error retrieving ledgers by parent type: {str(e)}")
            return Response(
                {'error': f'Error retrieving ledgers: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


@extend_schema(tags=['Parent Ledgers'])
class ParentLedgerViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet for getting ParentLedger options for TallyConfig forms"""
    serializer_class = ParentLedgerSerializer
    permission_classes = [OrganizationAPIKeyOrBearerToken]
    pagination_class = NoPagination  # Custom pagination that returns all data but keeps results format

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

    def get_queryset(self):
        """Filter parent ledgers by organization"""
        organization = self.get_organization()
        if not organization:
            return ParentLedger.objects.none()
            
        logger.info(f"Getting ParentLedgers for organization: {organization.name} (ID: {organization.id})")
        
        queryset = ParentLedger.objects.filter(organization=organization).order_by('parent')
        logger.info(f"Returning {queryset.count()} ParentLedgers")
        return queryset
   

    def dispatch(self, request, *args, **kwargs):
        """Intercept all incoming calls for logging and debugging"""
        # Log the incoming request
        logger.info(f"LedgerViewSet - {request.method} {request.get_full_path()}")
        logger.info(f"Request Data: {request.data if hasattr(request, 'data') else 'No data'}")

        # Get organization info for debugging
        try:
            org = self.get_organization()
            logger.info(f"Organization: {org.name if org else 'None'} (ID: {org.id if org else 'None'})")
        except Exception as e:
            logger.error(f"Error getting organization: {str(e)}")

        return super().dispatch(request, *args, **kwargs)

    @extend_schema(
        summary="Get Ledgers by Parent Type",
        description="Get all ledgers for a specific parent type from TallyConfig (igst_parents, cgst_parents, sgst_parents, vendor_parents, chart_of_accounts_parents, chart_of_accounts_expense_parents)",
        parameters=[
            OpenApiParameter(
                name='parent_type',
                description='Type of parent ledger to retrieve (igst_parents, cgst_parents, sgst_parents, vendor_parents, chart_of_accounts_parents, chart_of_accounts_expense_parents)',
                required=True,
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY
            ),
            OpenApiParameter(
                name='config_id',
                description='Specific TallyConfig ID to get ledgers from (optional - if not provided, uses first config)',
                required=False,
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.QUERY
            )
        ],
        responses={200: LedgerSerializer(many=True)},
    )
    @action(detail=False, methods=['get'], url_path='ledgers')
    def get_ledgers_by_parent_type(self, request, org_id=None):
        """Get ledgers by parent type from TallyConfig"""
        parent_type = request.query_params.get('parent_type')
        config_id = request.query_params.get('config_id')

        if not parent_type:
            return Response(
                {'error': 'parent_type parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Valid parent types
        valid_parent_types = [
            'igst_parents',
            'cgst_parents',
            'sgst_parents',
            'vendor_parents',
            'chart_of_accounts_parents',
            'chart_of_accounts_expense_parents',
            'tds_parents',
            'payment_parents'
        ]

        if parent_type not in valid_parent_types:
            return Response(
                {
                    'error': f'Invalid parent_type. Must be one of: {", ".join(valid_parent_types)}'
                },
                status=status.HTTP_400_BAD_REQUEST
            )

        organization = self.get_organization()
        if not organization:
            return Response(
                {'error': 'Organization not found'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            # Get TallyConfig - either specific one or first available
            if config_id:
                tally_config = TallyConfig.objects.get(
                    id=config_id,
                    organization=organization
                )
            else:
                tally_config = TallyConfig.objects.filter(
                    organization=organization
                ).first()

            if not tally_config:
                return Response(
                    {'error': 'No TallyConfig found for this organization'},
                    status=status.HTTP_404_NOT_FOUND
                )

            # Get the parent ledgers for the specified type
            parent_ledgers = getattr(tally_config, parent_type).all()

            if not parent_ledgers.exists():
                return Response(
                    {
                        'message': f'No {parent_type} configured in TallyConfig',
                        'config_id': str(tally_config.id),
                        'parent_type': parent_type,
                        'ledgers': []
                    },
                    status=status.HTTP_200_OK
                )

            # Get all ledgers under these parent ledgers
            ledgers = Ledger.objects.filter(
                parent__in=parent_ledgers,
                organization=organization
            ).select_related('parent').order_by('parent__parent', 'name')

            # Group ledgers by parent for better organization
            grouped_ledgers = {}
            total_ledgers = 0

            for ledger in ledgers:
                parent_name = ledger.parent.parent
                parent_id = str(ledger.parent.id)

                if parent_name not in grouped_ledgers:
                    grouped_ledgers[parent_name] = {
                        'parent_id': parent_id,
                        'parent_name': parent_name,
                        'ledger_count': 0,
                        'ledgers': []
                    }

                ledger_data = {
                    'id': str(ledger.id),
                    'master_id': ledger.master_id,
                    'alter_id': ledger.alter_id,
                    'name': ledger.name,
                    'alias': ledger.alias,
                    'opening_balance': str(ledger.opening_balance),
                    'gst_in': ledger.gst_in,
                    'company': ledger.company
                }

                grouped_ledgers[parent_name]['ledgers'].append(ledger_data)
                grouped_ledgers[parent_name]['ledger_count'] += 1
                total_ledgers += 1

            response_data = {
                'success': True,
                'config_id': str(tally_config.id),
                'parent_type': parent_type,
                'total_parent_ledgers': parent_ledgers.count(),
                'total_ledgers': total_ledgers,
                'grouped_ledgers': grouped_ledgers
            }

            logger.info(f"Retrieved {total_ledgers} ledgers for {parent_type} from {parent_ledgers.count()} parent ledgers")
            return Response(response_data, status=status.HTTP_200_OK)

        except TallyConfig.DoesNotExist:
            return Response(
                {'error': 'TallyConfig not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        except Exception as e:
            logger.error(f"Error retrieving ledgers by parent type: {str(e)}")
            return Response(
                {'error': f'Error retrieving ledgers: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @extend_schema(
        summary="List Tally Configurations",
        description="Get all Tally configurations for the organization",
    )
    def list(self, request, *args, **kwargs):
        logger.info(f"LedgerViewSet.list called for org: {self.get_organization()}")
        return super().list(request, *args, **kwargs)

    @extend_schema(
        summary="Create Tally Configuration",
        description="Create a new Tally configuration for mapping parent ledgers to different GST types and categories. Accepts bulk data ingestion.",
    )
    def create(self, request, *args, **kwargs):
        logger.info(f"LedgerViewSet.create called with data: {request.data}")
        organization = self.get_organization()
        logger.info(f"Creating TallyConfig for organization: {organization}")

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
                        logger.info(f"Created config: {config.id}")
                    else:
                        errors.append({
                            'data': config_data,
                            'errors': serializer.errors
                        })
                        logger.error(f"Validation errors for config: {serializer.errors}")
                except Exception as e:
                    errors.append({
                        'data': config_data,
                        'error': str(e)
                    })
                    logger.error(f"Error creating config: {str(e)}")

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
        logger.info(f"LedgerViewSet.retrieve called for config ID: {config_id}")
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(
        summary="Update Tally Configuration",
        description="Update a Tally configuration completely",
    )
    def update(self, request, *args, **kwargs):
        config_id = kwargs.get('pk')
        logger.info(f"LedgerViewSet.update called for config ID: {config_id} with data: {request.data}")
        return super().update(request, *args, **kwargs)

    @extend_schema(
        summary="Partially Update Tally Configuration",
        description="Partially update a Tally configuration",
    )
    def partial_update(self, request, *args, **kwargs):
        config_id = kwargs.get('pk')
        logger.info(f"LedgerViewSet.partial_update called for config ID: {config_id} with data: {request.data}")
        return super().partial_update(request, *args, **kwargs)

    @extend_schema(
        summary="Delete Tally Configuration",
        description="Delete a Tally configuration",
    )
    def destroy(self, request, *args, **kwargs):
        config_id = kwargs.get('pk')
        logger.info(f"LedgerViewSet.destroy called for config ID: {config_id}")
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
                logger.warning(f"Organization with ID {org_id} not found")
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

        logger.debug("No organization found, operating in test mode")
        return None

    def dispatch(self, request, *args, **kwargs):
        """Intercept all incoming calls for logging and debugging"""
        # Log the incoming request
        logger.info(f"LedgerViewSet - {request.method} {request.get_full_path()}")
        logger.info(f"Request Data: {request.data if hasattr(request, 'data') else 'No data'}")

        # Get organization info for debugging
        try:
            org = self.get_organization()
            logger.info(f"Organization: {org.name if org else 'TEST MODE - No Org'} (ID: {org.id if org else 'None'})")
        except Exception as e:
            logger.error(f"Error getting organization: {str(e)}")

        return super().dispatch(request, *args, **kwargs)

    @extend_schema(
        summary="List Ledgers",
        description="Get all ledgers for the organization grouped by parent ledger (No authentication required for testing)",
        responses={200: LedgerSerializer(many=True)},
    )
    def list(self, request, *args, **kwargs):
        """List all ledgers for the organization grouped by parent ledger"""
        logger.info(f"LedgerViewSet.list called - TEST MODE")
        queryset = self.get_queryset()
        logger.debug(f"Found {queryset.count()} ledgers")

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

        logger.info(f"Grouped into {len(grouped_ledgers)} parent categories")
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
        logger.info(f"Full Request URL: {full_url}")
        logger.info(f"LedgerViewSet.create called - TEST MODE")
        logger.debug(f"Raw request data type: {type(request.data)}")
        logger.debug(f"Raw request data keys: {list(request.data.keys()) if hasattr(request.data, 'keys') else 'No keys'}")

        organization = self.get_organization()
        if not organization:
            logger.debug("No organization found, using first available organization")
            try:
                organization = Organization.objects.first()
                if not organization:
                    logger.info("No organizations exist, this might cause issues")
                    return Response(
                        {'error': 'No organization available for testing. Please create an organization first.'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
                logger.info(f"Using organization: {organization.name} (ID: {organization.id})")
            except Exception as e:
                logger.error(f"Error getting organization: {str(e)}")
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
            logger.debug(f"Found LEDGER key with {len(ledger_data)} entries")
        elif isinstance(request.data, list):
            # If the data is directly a list, assume it's the ledger data
            ledger_data = request.data
            logger.info(f"Data is directly a list with {len(ledger_data)} entries")
        else:
            logger.info(f"Unexpected data format: {type(request.data)}")
            return Response(
                {'error': 'Invalid data format. Expected object with LEDGER key or array of ledgers.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not ledger_data:
            return Response(
                {'error': 'No Ledger Data Provided in LEDGER key'},
                status=status.HTTP_400_BAD_REQUEST
            )

        logger.debug(f"Processing {len(ledger_data)} ledger entries")
        created_ledgers = []
        failed_ledgers = []

        try:
            with transaction.atomic():
                for i, ledger_entry in enumerate(ledger_data):
                    try:
                        logger.debug(f"Processing ledger {i+1}: {ledger_entry.get('Name', 'Unknown')} with parent: {ledger_entry.get('Parent', 'Unknown')}")

                        parent_name = ledger_entry.get('Parent', '').strip()
                        if not parent_name:
                            parent_name = "Uncategorized"  # Default parent if empty

                        # Fetch or create ParentLedger
                        parent_ledger, created = ParentLedger.objects.get_or_create(
                            parent=parent_name,
                            organization=organization
                        )
                        if created:
                            logger.info(f"Created new parent ledger: {parent_name}")

                        # Clean and convert opening balance
                        opening_balance_str = str(ledger_entry.get('OpeningBalance', '0')).strip()
                        opening_balance = clean_decimal_value(opening_balance_str)

                        # Extract master_id for duplicate checking
                        master_id = ledger_entry.get('Master_Id', '').strip()

                        # Check for duplicate master_id within the same organization
                        if master_id:
                            existing_ledger = Ledger.objects.filter(
                                master_id=master_id,
                                organization=organization
                            ).first()

                            if existing_ledger:
                                logger.warning(f"Skipping duplicate ledger: master_id '{master_id}' already exists for organization '{organization.name}' (existing ledger: '{existing_ledger.name}')")
                                failed_ledgers.append({
                                    'index': i+1,
                                    'name': ledger_entry.get('Name', 'Unknown'),
                                    'master_id': master_id,
                                    'error': f'Duplicate master_id: {master_id} already exists for organization {organization.name}',
                                    'existing_ledger': existing_ledger.name,
                                    'data': ledger_entry
                                })
                                continue  # Skip creating this ledger and move to next

                        # Create Ledger instance (only if no duplicate found)
                        ledger_instance = Ledger.objects.create(
                            master_id=master_id,
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

                        logger.info(f"Successfully created ledger: {ledger_instance.name}")

                    except Exception as ledger_error:
                        logger.error(f"Error creating individual ledger {i+1}: {str(ledger_error)}")
                        failed_ledgers.append({
                            'index': i+1,
                            'name': ledger_entry.get('Name', 'Unknown'),
                            'error': str(ledger_error),
                            'data': ledger_entry
                        })
                        # Continue processing other ledgers instead of failing the entire transaction
                        continue

            logger.info(f"Successfully created {len(created_ledgers)} ledgers")
            logger.error(f"Failed to create {len(failed_ledgers)} ledgers")

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
            logger.error(f"Error in bulk creation transaction: {str(e)}")
            return Response({
                'error': f'Bulk creation failed: {str(e)}',
                'created_count': len(created_ledgers),
                'failed_count': len(failed_ledgers)
            }, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(tags=['Tally TCP'])
class MasterAPIView(APIView):
    """
    Master API View for processing incoming data from Tally
    Processes STOCKITEM data and saves directly to database
    OPEN FOR TESTING - NO AUTHENTICATION REQUIRED
    """
    permission_classes = [AllowAny]

    def get_organization(self):
        """Get organization from URL UUID parameter or API key (similar to LedgerViewSet)"""
        # Extract organization UUID from URL
        org_id = self.kwargs.get('org_id')
        if org_id:
            try:
                return Organization.objects.get(id=org_id)
            except Organization.DoesNotExist:
                logger.warning(f"Organization with ID {org_id} not found")
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

        logger.debug("No organization found, operating in test mode")
        return None

    def dispatch(self, request, *args, **kwargs):
        """Intercept all incoming calls for logging and debugging"""
        # Log the incoming request
        logger.info(f"MasterAPIView - {request.method} {request.get_full_path()}")
        logger.info(f"Request Data: {request.data if hasattr(request, 'data') else 'No data'}")

        # Get organization info for debugging
        try:
            org = self.get_organization()
            logger.info(f"Organization: {org.name if org else 'TEST MODE - No Org'} (ID: {org.id if org else 'None'})")
        except Exception as e:
            logger.error(f"Error getting organization: {str(e)}")

        return super().dispatch(request, *args, **kwargs)

    @extend_schema(
        summary="Fetch Master Data / Organization Info",
        description="Returns basic organization details and optionally stock items for testing.",
        responses={200: {'description': 'Data retrieved successfully'}}
    )
    def get(self, request, *args, **kwargs):
        """
        GET endpoint for testing / retrieving data.
        Returns organization info and (optionally) stock items.
        """
        try:
            organization = self.get_organization()
            if not organization:
                organization = Organization.objects.first()

            if not organization:
                return Response({
                    'success': False,
                    'error': 'No organization found in system'
                }, status=status.HTTP_404_NOT_FOUND)

            # Query stock items for this org (limit for safety)
            stock_items_qs = StockItem.objects.filter(organization=organization).all()
            stock_items_data = [
                {
                    'id': str(item.id),
                    'master_id': item.master_id,
                    'alter_id': item.alter_id,
                    'name': item.name,
                    'parent': item.parent,
                    'unit': item.unit,
                    'category': item.category,
                    'gst_applicable': item.gst_applicable,
                    'item_code': item.item_code,
                    'alias': item.alias,
                    'company': item.company
                }
                for item in stock_items_qs
            ]

            return Response({
                'success': True,
                'message': 'Data retrieved successfully',
                'organization': {
                    'id': str(organization.id),
                    'name': organization.name
                },
                'stock_items': stock_items_data,
                'stock_items_count': stock_items_qs.count()
            }, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"Error in MasterAPIView GET: {str(e)}")
            return Response({
                'success': False,
                'error': f'Failed to retrieve data: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @extend_schema(
        summary="Process Master Data from Tally",
        description="Receives raw data from Tally and processes it directly to database. Currently supports STOCKITEM data processing.",
        responses={200: {'description': 'Data processed successfully'}},
    )
    def post(self, request, *args, **kwargs):
        """
        Process incoming raw data from Tally and save to database
        Organization-scoped data processing
        """
        try:
            # Get organization
            organization = self.get_organization()
            if not organization:
                logger.debug("No organization found, using first available organization")
                try:
                    organization = Organization.objects.first()
                    if not organization:
                        logger.info("No organizations exist, this might cause issues")
                        # Continue without organization for testing
                        organization = None
                    else:
                        logger.info(f"Using organization: {organization.name} (ID: {organization.id})")
                except Exception as e:
                    logger.error(f"Error getting organization: {str(e)}")
                    organization = None

            # Get raw request body
            raw_data = request.body.decode('utf-8')

            # Log incoming request details
            logger.info(f"MasterAPIView - POST request received")
            logger.info(f"Organization: {organization.name if organization else 'TEST MODE'}")
            logger.debug(f"Raw Data Length: {len(raw_data)} characters")

            # Parse JSON data for processing
            try:
                import json
                parsed_data = json.loads(raw_data) if raw_data else {}
                data_keys = list(parsed_data.keys()) if isinstance(parsed_data, dict) else []
                logger.info(f"JSON Data Keys: {data_keys}")

                # Log data structure info
                if isinstance(parsed_data, dict):
                    logger.info(f"Data structure: Dictionary with {len(parsed_data)} keys")
                elif isinstance(parsed_data, list):
                    logger.info(f"Data structure: Array with {len(parsed_data)} items")
                else:
                    logger.info(f"Data structure: {type(parsed_data)}")

                # Process STOCKITEM data if present
                stockitem_processing_result = None
                if isinstance(parsed_data, dict) and 'STOCKITEM' in parsed_data:
                    stockitem_processing_result = self.process_stockitem_data(parsed_data['STOCKITEM'], organization)

            except json.JSONDecodeError:
                logger.debug("Raw data is not valid JSON")
                return Response({
                    'error': 'Invalid JSON data provided',
                    'success': False
                }, status=status.HTTP_400_BAD_REQUEST)
            except Exception as e:
                logger.error(f"Error parsing data: {str(e)}")
                return Response({
                    'error': f'Error processing data: {str(e)}',
                    'success': False
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

            # Prepare response data
            response_data = {
                'success': True,
                'message': 'Data processed successfully',
                'organization': {
                    'id': str(organization.id) if organization else None,
                    'name': organization.name if organization else 'TEST MODE'
                },
                'data_length': len(raw_data),
                'processed_data_types': data_keys
            }

            # Add STOCKITEM processing results if any
            if stockitem_processing_result:
                response_data['stockitem_processing'] = stockitem_processing_result

            return Response(response_data, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"Error in MasterAPIView: {str(e)}")
            return Response({
                'error': f'Failed to process incoming data: {str(e)}',
                'success': False
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def process_stockitem_data(self, stockitem_data, organization):
        """
        Process STOCKITEM data and save to database
        Args:
            stockitem_data: List of stock item dictionaries from Tally
            organization: Organization instance
        Returns:
            Dictionary with processing results
        """
        if not stockitem_data or not isinstance(stockitem_data, list):
            return {
                'success': False,
                'error': 'No valid STOCKITEM data provided'
            }

        if not organization:
            logger.debug("No organization available for STOCKITEM processing")
            return {
                'success': False,
                'error': 'No organization available for processing'
            }

        logger.debug(f"Processing {len(stockitem_data)} STOCKITEM entries")
        created_items = []
        failed_items = []
        updated_items = []

        try:
            with transaction.atomic():
                for i, item_entry in enumerate(stockitem_data):
                    try:
                        logger.debug(f"Processing stock item {i+1}: {item_entry.get('Name', 'Unknown')}")

                        # Clean the company field (remove extra whitespace and newlines)
                        company = item_entry.get('Company', '').strip().replace('\r\n', '').replace('\n', '')

                        # Create or update StockItem instance
                        stock_item_data = {
                            'master_id': item_entry.get('Master_Id', ''),
                            'alter_id': item_entry.get('Alter_id', ''),
                            'name': item_entry.get('Name', ''),
                            'parent': item_entry.get('Parent', ''),
                            'unit': item_entry.get('Unit', ''),
                            'category': item_entry.get('Category', ''),
                            'gst_applicable': item_entry.get('GstApplicable', ''),
                            'item_code': item_entry.get('Item_Code', ''),
                            'alias': item_entry.get('ALIAS', ''),
                            'company': company,
                            'organization': organization
                        }

                        # Try to find existing stock item to update or create new one
                        master_id = item_entry.get('Master_Id', '')
                        if master_id and organization:
                            stock_item, created = StockItem.objects.update_or_create(
                                master_id=master_id,
                                organization=organization,
                                defaults=stock_item_data
                            )
                        else:
                            # If no master_id, create new item
                            stock_item = StockItem.objects.create(**stock_item_data)
                            created = True

                        # Prepare response data
                        item_response = {
                            'id': str(stock_item.id),
                            'master_id': stock_item.master_id,
                            'alter_id': stock_item.alter_id,
                            'name': stock_item.name,
                            'parent': stock_item.parent,
                            'unit': stock_item.unit,
                            'category': stock_item.category,
                            'gst_applicable': stock_item.gst_applicable,
                            'item_code': stock_item.item_code,
                            'alias': stock_item.alias,
                            'company': stock_item.company
                        }

                        if created:
                            created_items.append(item_response)
                            logger.info(f"Successfully created stock item: {stock_item.name}")
                        else:
                            updated_items.append(item_response)
                            logger.info(f"Successfully updated stock item: {stock_item.name}")

                    except Exception as item_error:
                        logger.error(f"Error processing stock item {i+1}: {str(item_error)}")
                        failed_items.append({
                            'index': i+1,
                            'name': item_entry.get('Name', 'Unknown'),
                            'error': str(item_error),
                            'data': item_entry
                        })
                        # Continue processing other items instead of failing the entire transaction
                        continue

            logger.info(f"Successfully created {len(created_items)} stock items")
            logger.info(f"Successfully updated {len(updated_items)} stock items")
            logger.error(f"Failed to process {len(failed_items)} stock items")

            # Return detailed response
            return {
                'success': True,
                'created_count': len(created_items),
                'updated_count': len(updated_items),
                'failed_count': len(failed_items),
                'total_processed': len(stockitem_data),
                'created_items': created_items[:5],  # Limit to first 5 for response size
                'updated_items': updated_items[:5],  # Limit to first 5 for response size
                'failed_items': failed_items[:3] if failed_items else []  # Limit failed entries in response
            }

        except Exception as e:
            logger.error(f"Error in STOCKITEM bulk processing transaction: {str(e)}")
            return {
                'success': False,
                'error': f'STOCKITEM bulk processing failed: {str(e)}',
                'created_count': len(created_items),
                'updated_count': len(updated_items),
                'failed_count': len(failed_items)
            }


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
        logger.error(f"Error cleaning decimal value '{value_str}': {str(e)}")
        return '0.00'


@extend_schema(
    tags=['Tally Bill Sync'],
    request={
        'application/json': {
            'type': 'object',
            'properties': {
                'id': {'type': 'string', 'format': 'uuid', 'description': 'Bill ID to update'},
                'status': {'type': 'boolean', 'description': 'Tally sync status to set'},
                'mode': {'type': 'string', 'enum': ['vendor', 'expense'], 'description': 'Bill type mode'}
            },
            'required': ['id', 'status', 'mode']
        }
    },
    responses={
        200: {
            'type': 'object',
            'properties': {
                'success': {'type': 'boolean'},
                'message': {'type': 'string'},
                'data': {
                    'type': 'object',
                    'properties': {
                        'id': {'type': 'string'},
                        'tally_synced': {'type': 'boolean'},
                        'mode': {'type': 'string'}
                    }
                }
            }
        },
        400: {
            'type': 'object',
            'properties': {
                'success': {'type': 'boolean'},
                'message': {'type': 'string'}
            }
        },
        404: {
            'type': 'object',
            'properties': {
                'success': {'type': 'boolean'},
                'message': {'type': 'string'}
            }
        }
    }
)
import logging

logger = logging.getLogger(__name__)

@api_view(['POST'])
@permission_classes([OrganizationAPIKeyOrBearerToken])
def update_bill_tally_sync_status(request, org_id):
    """
    Update the tally_synced status for bills based on mode (vendor or expense).
    Supports both single and bulk updates.

    Accepts POST request with:
    Single update format:
    - id: UUID of the bill to update
    - status: Boolean value or string (True/true/TRUE/False/false/FALSE) to set for tally_synced field
    - mode: String 'vendor' or 'expense' to determine which model to use

    Bulk update format:
    - Data: Array of objects with id, status, and mode fields
    """
    try:
        # Get and validate organization
        organization = get_organization_from_request(request, org_id)
        if not organization:
            return Response({
                'success': False,
                'message': f'Organization with ID {org_id} not found or you do not have access to it'
            }, status=status.HTTP_404_NOT_FOUND)

        # Helper function to normalize status to boolean
        def normalize_status(status_value):
            if isinstance(status_value, bool):
                return status_value
            if isinstance(status_value, str):
                if status_value.lower() in ['true', '1']:
                    return True
                elif status_value.lower() in ['false', '0']:
                    return False
                else:
                    raise ValueError(f"Invalid status string: {status_value}")
            raise ValueError(f"Status must be boolean or string, got {type(status_value)}")

        # Helper function to validate and process a single bill update
        def process_single_bill(bill_data):
            bill_id = bill_data.get('id')
            sync_status = bill_data.get('status')
            mode = bill_data.get('mode')

            # Validate required fields
            if not all([bill_id is not None, sync_status is not None, mode is not None]):
                return {'error': 'Missing required fields: id, status, and mode are all required'}

            # Validate mode
            if mode not in ['vendor', 'expense']:
                return {'error': 'Invalid mode. Must be either "vendor" or "expense"'}

            # Normalize and validate status
            try:
                sync_status = normalize_status(sync_status)
            except ValueError as e:
                return {'error': str(e)}

            # Select the appropriate model based on mode
            model = TallyVendorBill if mode == 'vendor' else TallyExpenseBill

            # Find the bill by ID
            try:
                bill = model.objects.get(id=bill_id)
            except model.DoesNotExist:
                return {'error': f'{mode.capitalize()} bill with ID {bill_id} not found'}
            except ValueError:
                return {'error': f'Invalid UUID format for bill ID {bill_id}'}

            # Update the tally_synced status
            bill.tally_synced = sync_status
            bill.save()

            return {
                'success': True,
                'id': str(bill.id),
                'tally_synced': bill.tally_synced,
                'mode': mode
            }

        # Check if this is a bulk update request
        bulk_data = request.data.get('Data')
        if bulk_data is not None:
            # Handle bulk update
            if not isinstance(bulk_data, list):
                return Response({
                    'success': False,
                    'message': 'Data must be an array for bulk updates'
                }, status=status.HTTP_400_BAD_REQUEST)

            results = []
            errors = []

            for i, bill_data in enumerate(bulk_data):
                result = process_single_bill(bill_data)
                if 'error' in result:
                    errors.append({
                        'index': i,
                        'id': bill_data.get('id', 'unknown'),
                        'error': result['error']
                    })
                else:
                    results.append(result)

            # Return response with results and errors
            response_data = {
                'success': len(errors) == 0,
                'updated_count': len(results),
                'error_count': len(errors),
                'results': results
            }

            if errors:
                response_data['errors'] = errors

            return Response(response_data, status=status.HTTP_200_OK if len(errors) == 0 else status.HTTP_207_MULTI_STATUS)

        else:
            # Handle single update (backward compatibility)
            result = process_single_bill(request.data)
            if 'error' in result:
                return Response({
                    'success': False,
                    'message': result['error']
                }, status=status.HTTP_400_BAD_REQUEST)

            return Response({
                'success': True,
                'message': f'{result["mode"].capitalize()} bill tally sync status updated successfully',
                'data': {
                    'id': result['id'],
                    'tally_synced': result['tally_synced'],
                    'mode': result['mode']
                }
            }, status=status.HTTP_200_OK)

    except Exception as e:
        return Response({
            'success': False,
            'message': f'Error updating bill tally sync status: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


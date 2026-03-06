# apps/module/tally/views/parent_ledger.py
"""
ParentLedger read-only views (ParentLedgerViewSet).
"""
import logging

from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, OpenApiParameter
from drf_spectacular.openapi import OpenApiTypes
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.organizations.models import Organization

from ..models import Ledger, ParentLedger, TallyConfig
from ..serializers import ParentLedgerSerializer, LedgerSerializer
from .helpers import NoPagination, OrganizationAPIKeyOrBearerToken

logger = logging.getLogger(__name__)


@extend_schema(tags=["Parent Ledgers"])
class ParentLedgerViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet for getting ParentLedger options for TallyConfig forms."""

    serializer_class = ParentLedgerSerializer
    permission_classes = [OrganizationAPIKeyOrBearerToken]
    pagination_class = NoPagination

    def _get_organization(self):
        org_id = self.kwargs.get("org_id")
        if org_id:
            return get_object_or_404(Organization, id=org_id)
        if hasattr(self.request, "organization"):
            return self.request.organization
        if hasattr(self.request.user, "memberships"):
            membership = self.request.user.memberships.first()
            if membership:
                return membership.organization
        return None

    # Alias for backward compat
    get_organization = _get_organization

    def get_queryset(self):
        organization = self._get_organization()
        if not organization:
            return ParentLedger.objects.none()
        return ParentLedger.objects.filter(organization=organization).order_by("parent")

    def dispatch(self, request, *args, **kwargs):
        logger.info(f"ParentLedgerViewSet - {request.method} {request.get_full_path()}")
        return super().dispatch(request, *args, **kwargs)

    @extend_schema(
        summary="Get Ledgers by Parent Type",
        description="Get all ledgers for a specific parent type from TallyConfig",
        parameters=[
            OpenApiParameter(name="parent_type", required=True, type=OpenApiTypes.STR, location=OpenApiParameter.QUERY),
            OpenApiParameter(name="config_id", required=False, type=OpenApiTypes.UUID, location=OpenApiParameter.QUERY),
        ],
        responses={200: LedgerSerializer(many=True)},
    )
    @action(detail=False, methods=["get"], url_path="ledgers")
    def get_ledgers_by_parent_type(self, request, org_id=None):
        """Get ledgers by parent type from TallyConfig."""
        parent_type = request.query_params.get("parent_type")
        config_id = request.query_params.get("config_id")

        if not parent_type:
            return Response({"error": "parent_type parameter is required"}, status=status.HTTP_400_BAD_REQUEST)

        valid_parent_types = [
            "igst_parents", "cgst_parents", "sgst_parents", "vendor_parents",
            "chart_of_accounts_parents", "chart_of_accounts_expense_parents",
            "tds_parents", "payment_parents",
        ]
        if parent_type not in valid_parent_types:
            return Response(
                {"error": f'Invalid parent_type. Must be one of: {", ".join(valid_parent_types)}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        organization = self._get_organization()
        if not organization:
            return Response({"error": "Organization not found"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            if config_id:
                tally_config = TallyConfig.objects.get(id=config_id, organization=organization)
            else:
                tally_config = TallyConfig.objects.filter(organization=organization).first()

            if not tally_config:
                return Response({"error": "No TallyConfig found for this organization"}, status=status.HTTP_404_NOT_FOUND)

            parent_ledgers = getattr(tally_config, parent_type).all()

            if not parent_ledgers.exists():
                return Response(
                    {
                        "message": f"No {parent_type} configured in TallyConfig",
                        "config_id": str(tally_config.id),
                        "parent_type": parent_type,
                        "ledgers": [],
                    },
                    status=status.HTTP_200_OK,
                )

            ledgers = (
                Ledger.objects.filter(parent__in=parent_ledgers, organization=organization)
                .select_related("parent")
                .order_by("parent__parent", "name")
            )

            grouped_ledgers = {}
            total_ledgers = 0
            for ledger in ledgers:
                parent_name = ledger.parent.parent
                parent_id = str(ledger.parent.id)
                if parent_name not in grouped_ledgers:
                    grouped_ledgers[parent_name] = {
                        "parent_id": parent_id,
                        "parent_name": parent_name,
                        "ledger_count": 0,
                        "ledgers": [],
                    }
                grouped_ledgers[parent_name]["ledgers"].append(
                    {
                        "id": str(ledger.id),
                        "master_id": ledger.master_id,
                        "alter_id": ledger.alter_id,
                        "name": ledger.name,
                        "alias": ledger.alias,
                        "opening_balance": str(ledger.opening_balance),
                        "gst_in": ledger.gst_in,
                        "company": ledger.company,
                    }
                )
                grouped_ledgers[parent_name]["ledger_count"] += 1
                total_ledgers += 1

            return Response(
                {
                    "success": True,
                    "config_id": str(tally_config.id),
                    "parent_type": parent_type,
                    "total_parent_ledgers": parent_ledgers.count(),
                    "total_ledgers": total_ledgers,
                    "grouped_ledgers": grouped_ledgers,
                },
                status=status.HTTP_200_OK,
            )

        except TallyConfig.DoesNotExist:
            return Response({"error": "TallyConfig not found"}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            logger.error(f"Error retrieving ledgers by parent type: {str(e)}")
            return Response(
                {"error": f"Error retrieving ledgers: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

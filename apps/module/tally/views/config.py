# apps/module/tally/views/config.py
"""
Tally configuration views – get/create/update tally config and
TallyConfigViewSet (ledgers-by-parent-type action).
"""
import logging

from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, OpenApiParameter
from drf_spectacular.openapi import OpenApiTypes
from rest_framework import viewsets, status
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.response import Response

from apps.common.permissions import IsOrgAdmin
from apps.common.utils import get_organization_from_request
from apps.organizations.models import Organization

from decimal import Decimal, InvalidOperation

from ..models import Ledger, ParentLedger, TallyConfig, GstRateLedgerMapping
from ..serializers import (
    LedgerSerializer,
    TallyConfigSerializer,
    GstRateLedgerMappingSerializer,
)
from .helpers import OrganizationAPIKeyOrBearerToken

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Function-based config views
# ---------------------------------------------------------------------------

@extend_schema(tags=["Tally Config"])
@api_view(["GET"])
@permission_classes([OrganizationAPIKeyOrBearerToken])
def get_tally_config(request, org_id):
    """Get tally configuration for organization."""
    logger.info(f"get_tally_config called - Method: {request.method}, org_id: {org_id}")

    try:
        organization = get_object_or_404(Organization, id=org_id)

        tally_config = (
            TallyConfig.objects.filter(organization=organization)
            .prefetch_related(
                "igst_parents",
                "cgst_parents",
                "sgst_parents",
                "vendor_parents",
                "chart_of_accounts_parents",
                "chart_of_accounts_expense_parents",
                "tds_parents",
                "payment_parents",
            )
            .first()
        )

        if not tally_config:
            return Response(
                {"success": False, "message": "No tally configuration found for this organization", "data": None},
                status=status.HTTP_200_OK,
            )

        serializer = TallyConfigSerializer(tally_config, context={"request": request, "organization": organization})
        return Response(
            {"success": True, "message": "Tally configuration retrieved successfully", "data": serializer.data},
            status=status.HTTP_200_OK,
        )

    except Organization.DoesNotExist:
        return Response({"success": False, "message": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        logger.error(f"Error in get_tally_config: {str(e)}")
        return Response(
            {"success": False, "message": f"Error retrieving tally configuration: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@extend_schema(tags=["Tally Config"])
@api_view(["POST"])
@permission_classes([OrganizationAPIKeyOrBearerToken])
def create_or_update_tally_config(request, org_id):
    """Create or update tally configuration for organization."""
    logger.info(f"create_or_update_tally_config called - Method: {request.method}, org_id: {org_id}")

    try:
        organization = get_object_or_404(Organization, id=org_id)

        parent_fields = [
            "igst_parents",
            "cgst_parents",
            "sgst_parents",
            "vendor_parents",
            "chart_of_accounts_parents",
            "chart_of_accounts_expense_parents",
            "tds_parents",
            "payment_parents",
        ]

        for field in parent_fields:
            if field in request.data:
                parent_ids = request.data[field]
                if parent_ids:
                    existing = ParentLedger.objects.filter(id__in=parent_ids, organization=organization).values_list(
                        "id", "parent"
                    )
                    existing_ids = [str(pid) for pid, _ in existing]
                    missing_ids = [pid for pid in parent_ids if str(pid) not in existing_ids]
                    if missing_ids:
                        logger.warning(f"  Missing ParentLedger IDs for {field}: {missing_ids}")

        existing_config = TallyConfig.objects.filter(organization=organization).first()
        tally_product_allow_sync = request.data.get("tally_product_allow_sync", False)

        if existing_config:
            tally_config = existing_config
        else:
            tally_config = TallyConfig.objects.create(
                organization=organization, tally_product_allow_sync=tally_product_allow_sync
            )

        tally_config.tally_product_allow_sync = tally_product_allow_sync
        tally_config.save()

        for field in parent_fields:
            if field in request.data:
                parent_ids = request.data[field] or []
                parent_ledgers = ParentLedger.objects.filter(id__in=parent_ids, organization=organization)
                getattr(tally_config, field).set(parent_ledgers)

        tally_config.refresh_from_db()

        context = {"request": request, "organization": organization}
        response_serializer = TallyConfigSerializer(tally_config, context=context)

        message = (
            "Tally configuration updated successfully" if existing_config else "Tally configuration created successfully"
        )
        return Response(
            {"success": True, "message": message, "data": response_serializer.data},
            status=status.HTTP_200_OK if existing_config else status.HTTP_201_CREATED,
        )

    except Organization.DoesNotExist:
        return Response({"success": False, "message": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        logger.error(f"Error in create_or_update_tally_config: {str(e)}")
        import traceback

        traceback.print_exc()
        return Response(
            {"success": False, "message": f"Error saving tally configuration: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


# ---------------------------------------------------------------------------
# TallyConfigViewSet – kept as ViewSet since it uses router registration with
# custom actions (ledgers-by-parent-type).  Planned conversion to FBVs later.
# ---------------------------------------------------------------------------

@extend_schema(tags=["Tally Config ViewSet"])
class TallyConfigViewSet(viewsets.ModelViewSet):
    """ViewSet for TallyConfig with ledgers action."""

    serializer_class = TallyConfigSerializer
    permission_classes = [OrganizationAPIKeyOrBearerToken]

    def get_queryset(self):
        organization = self._get_organization()
        return (
            TallyConfig.objects.filter(organization=organization)
            .prefetch_related(
                "igst_parents",
                "cgst_parents",
                "sgst_parents",
                "vendor_parents",
                "chart_of_accounts_parents",
                "chart_of_accounts_expense_parents",
                "tds_parents",
                "payment_parents",
            )
            .order_by("-id")
        )

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

    # Alias for backward compatibility
    get_organization = _get_organization

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
            return Response({"error": f"Error retrieving ledgers: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    def create(self, request, *args, **kwargs):
        organization = self._get_organization()
        if isinstance(request.data, list):
            created_configs, errors = [], []
            for config_data in request.data:
                try:
                    serializer = self.get_serializer(data=config_data)
                    if serializer.is_valid():
                        serializer.save(organization=organization)
                        created_configs.append(serializer.data)
                    else:
                        errors.append({"data": config_data, "errors": serializer.errors})
                except Exception as e:
                    errors.append({"data": config_data, "error": str(e)})
            response_data = {
                "created": created_configs,
                "errors": errors,
                "created_count": len(created_configs),
                "error_count": len(errors),
            }
            if errors:
                return Response(response_data, status=status.HTTP_207_MULTI_STATUS)
            return Response(response_data, status=status.HTTP_201_CREATED)
        return super().create(request, *args, **kwargs)

    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        return super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        return super().partial_update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        return super().destroy(request, *args, **kwargs)


# ---------------------------------------------------------------------------
# GST Rate → Ledger Mapping endpoints
# ---------------------------------------------------------------------------


@extend_schema(tags=["Tally Config"])
@api_view(["GET"])
@permission_classes([OrganizationAPIKeyOrBearerToken])
def list_gst_rate_ledger_mappings(request, org_id):
    """List per-rate CGST/SGST/IGST ledger mappings for an organization."""
    organization = get_object_or_404(Organization, id=org_id)
    mappings = GstRateLedgerMapping.objects.filter(organization=organization).order_by("rate")
    serializer = GstRateLedgerMappingSerializer(mappings, many=True)
    return Response(
        {"success": True, "data": serializer.data},
        status=status.HTTP_200_OK,
    )


@extend_schema(tags=["Tally Config"])
@api_view(["POST"])
@permission_classes([OrganizationAPIKeyOrBearerToken])
def upsert_gst_rate_ledger_mappings(request, org_id):
    """
    Bulk upsert GST rate → ledger mappings.

    Payload: {"mappings": [
        {"rate": "5.00", "cgst_ledger": "<uuid>", "sgst_ledger": "<uuid>", "igst_ledger": "<uuid>"},
        ...
    ]}

    Each rate is unique per organization. Existing rows are updated; new rows are created.
    """
    organization = get_object_or_404(Organization, id=org_id)
    payload = request.data.get("mappings", [])
    if not isinstance(payload, list):
        return Response(
            {"success": False, "message": "`mappings` must be a list"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    saved, errors = [], []
    for entry in payload:
        try:
            rate_raw = entry.get("rate")
            try:
                rate = Decimal(str(rate_raw))
            except (InvalidOperation, TypeError):
                errors.append({"rate": rate_raw, "error": "Invalid rate"})
                continue

            cgst_id = entry.get("cgst_ledger") or None
            sgst_id = entry.get("sgst_ledger") or None
            igst_id = entry.get("igst_ledger") or None

            obj, _ = GstRateLedgerMapping.objects.update_or_create(
                organization=organization,
                rate=rate,
                defaults={
                    "cgst_ledger_id": cgst_id,
                    "sgst_ledger_id": sgst_id,
                    "igst_ledger_id": igst_id,
                },
            )
            saved.append(GstRateLedgerMappingSerializer(obj).data)
        except Exception as e:
            errors.append({"entry": entry, "error": str(e)})

    return Response(
        {"success": not errors, "saved": saved, "errors": errors},
        status=status.HTTP_200_OK if not errors else status.HTTP_207_MULTI_STATUS,
    )


@extend_schema(tags=["Tally Config"])
@api_view(["DELETE"])
@permission_classes([OrganizationAPIKeyOrBearerToken])
def delete_gst_rate_ledger_mapping(request, org_id, mapping_id):
    """Delete a single GST rate ledger mapping."""
    organization = get_object_or_404(Organization, id=org_id)
    mapping = get_object_or_404(GstRateLedgerMapping, id=mapping_id, organization=organization)
    mapping.delete()
    return Response({"success": True}, status=status.HTTP_200_OK)

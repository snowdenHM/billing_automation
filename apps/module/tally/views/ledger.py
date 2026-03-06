# apps/module/tally/views/ledger.py
"""
Ledger CRUD views (currently LedgerViewSet – open for testing, no auth).
"""
import logging

from django.db import transaction
from drf_spectacular.utils import extend_schema
from rest_framework import viewsets, status
from rest_framework.response import Response

from apps.organizations.models import Organization

from ..models import Ledger, ParentLedger
from ..serializers import LedgerSerializer, LedgerBulkCreateSerializer
from .helpers import clean_decimal_value

logger = logging.getLogger(__name__)


@extend_schema(tags=["Tally TCP"])
class LedgerViewSet(viewsets.GenericViewSet):
    """
    Simplified Ledger ViewSet with only GET and POST operations.
    OPEN FOR TESTING – NO AUTHENTICATION REQUIRED.
    """

    serializer_class = LedgerSerializer
    permission_classes = []

    def get_queryset(self):
        organization = self._get_organization()
        if organization:
            return Ledger.objects.filter(organization=organization).select_related("parent")
        return Ledger.objects.all().select_related("parent")

    def _get_organization(self):
        org_id = self.kwargs.get("org_id")
        if org_id:
            try:
                return Organization.objects.get(id=org_id)
            except Organization.DoesNotExist:
                return None
        if hasattr(self.request, "auth") and self.request.auth:
            from apps.organizations.models import OrganizationAPIKey
            try:
                return OrganizationAPIKey.objects.get(api_key=self.request.auth).organization
            except OrganizationAPIKey.DoesNotExist:
                pass
        if hasattr(self.request, "organization"):
            return self.request.organization
        if hasattr(self.request.user, "memberships") and self.request.user.is_authenticated:
            membership = self.request.user.memberships.first()
            if membership:
                return membership.organization
        return None

    # Alias for backward compat
    get_organization = _get_organization

    def dispatch(self, request, *args, **kwargs):
        logger.info(f"LedgerViewSet - {request.method} {request.get_full_path()}")
        return super().dispatch(request, *args, **kwargs)

    @extend_schema(
        summary="List Ledgers",
        description="Get all ledgers for the organization grouped by parent ledger",
        responses={200: LedgerSerializer(many=True)},
    )
    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()

        grouped_ledgers = {}
        for ledger in queryset:
            parent_name = ledger.parent.parent if ledger.parent else "Uncategorized"
            parent_id = str(ledger.parent.id) if ledger.parent else "uncategorized"
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

        return Response(
            {
                "success": True,
                "total_parents": len(grouped_ledgers),
                "total_ledgers": queryset.count(),
                "grouped_ledgers": grouped_ledgers,
            }
        )

    @extend_schema(
        summary="Bulk Create Ledgers from Tally",
        description="Create multiple ledgers from Tally data format.",
        request=LedgerBulkCreateSerializer,
        responses={201: LedgerSerializer(many=True)},
    )
    def create(self, request, *args, **kwargs):
        organization = self._get_organization()
        if not organization:
            try:
                organization = Organization.objects.first()
                if not organization:
                    return Response(
                        {"error": "No organization available. Please create one first."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            except Exception:
                return Response({"error": "Could not determine organization"}, status=status.HTTP_400_BAD_REQUEST)

        if not request.data:
            return Response({"error": "No data provided"}, status=status.HTTP_400_BAD_REQUEST)

        # Extract LEDGER data
        if isinstance(request.data, dict):
            ledger_data = request.data.get("LEDGER", [])
        elif isinstance(request.data, list):
            ledger_data = request.data
        else:
            return Response(
                {"error": "Invalid data format. Expected object with LEDGER key or array of ledgers."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not ledger_data:
            return Response({"error": "No Ledger Data Provided in LEDGER key"}, status=status.HTTP_400_BAD_REQUEST)

        created_ledgers, failed_ledgers = [], []

        try:
            with transaction.atomic():
                for i, entry in enumerate(ledger_data):
                    try:
                        parent_name = (entry.get("Parent", "") or "").strip() or "Uncategorized"
                        parent_ledger, _ = ParentLedger.objects.get_or_create(
                            parent=parent_name, organization=organization
                        )

                        opening_balance = clean_decimal_value(str(entry.get("OpeningBalance", "0")).strip())
                        master_id = (entry.get("Master_Id", "") or "").strip()

                        if master_id:
                            existing = Ledger.objects.filter(master_id=master_id, organization=organization).first()
                            if existing:
                                failed_ledgers.append(
                                    {
                                        "index": i + 1,
                                        "name": entry.get("Name", "Unknown"),
                                        "master_id": master_id,
                                        "error": f"Duplicate master_id: {master_id}",
                                        "existing_ledger": existing.name,
                                        "data": entry,
                                    }
                                )
                                continue

                        ledger_instance = Ledger.objects.create(
                            master_id=master_id,
                            alter_id=entry.get("Alter_id", "") or entry.get("Alter_Id", ""),
                            name=entry.get("Name", ""),
                            parent=parent_ledger,
                            alias=entry.get("ALIAS", ""),
                            opening_balance=opening_balance,
                            gst_in=entry.get("GSTIN", ""),
                            company=entry.get("Company", ""),
                            organization=organization,
                        )
                        created_ledgers.append(
                            {
                                "id": str(ledger_instance.id),
                                "master_id": ledger_instance.master_id,
                                "alter_id": ledger_instance.alter_id,
                                "name": ledger_instance.name,
                                "parent": ledger_instance.parent.parent,
                                "alias": ledger_instance.alias,
                                "opening_balance": str(ledger_instance.opening_balance),
                                "gst_in": ledger_instance.gst_in,
                                "company": ledger_instance.company,
                            }
                        )
                    except Exception as e:
                        failed_ledgers.append(
                            {"index": i + 1, "name": entry.get("Name", "Unknown"), "error": str(e), "data": entry}
                        )
                        continue

            response_data = {
                "success": True,
                "created_count": len(created_ledgers),
                "failed_count": len(failed_ledgers),
                "created_ledgers": created_ledgers,
                "failed_ledgers": failed_ledgers[:10] if failed_ledgers else [],
            }
            if failed_ledgers:
                return Response(response_data, status=status.HTTP_207_MULTI_STATUS)
            return Response(response_data, status=status.HTTP_201_CREATED)

        except Exception as e:
            logger.error(f"Error in bulk creation: {str(e)}")
            return Response(
                {"error": f"Bulk creation failed: {str(e)}", "created_count": len(created_ledgers), "failed_count": len(failed_ledgers)},
                status=status.HTTP_400_BAD_REQUEST,
            )

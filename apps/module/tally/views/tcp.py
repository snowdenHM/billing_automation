# apps/module/tally/views/tcp.py
from __future__ import annotations

import os
import re
from collections import defaultdict
from datetime import datetime
from typing import Optional

from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils.timezone import localtime
from drf_spectacular.utils import extend_schema, OpenApiParameter
from rest_framework import status, viewsets
from rest_framework.permissions import AllowAny, BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.organizations.models import Organization
from apps.module.tally.models import (
    Ledger,
    ParentLedger,
    TallyExpenseAnalyzedBill,
    TallyExpenseAnalyzedProduct,
    TallyVendorAnalyzedBill,
    TallyVendorAnalyzedProduct,
)
from apps.module.tally.serializers.tcp import (
    LedgerSerializer,
    TallyLedgerPayloadSerializer,
    MasterPayloadSerializer,
    ExpenseSyncedResponseSerializer,
    VendorSyncedResponseSerializer,
)
# rest_framework_api_key model you created for org-scoped keys
try:
    from apps.organizations.models import OrganizationAPIKey  # your subclass of AbstractAPIKey
except Exception:  # fallback if name differs
    OrganizationAPIKey = None


# --------------------------
# Permission: Org API Key
# --------------------------

class HasOrgAPIKey(BasePermission):
    """
    Requires header 'X-API-Key: <key>'.
    The key must belong to the Organization referenced by URL kwarg 'org_id'.
    """

    keyword = "X-API-Key"

    def has_permission(self, request, view):
        if request.method == "OPTIONS":
            return True

        api_key = request.headers.get(self.keyword) or request.META.get(f"HTTP_{self.keyword.replace('-', '_')}")
        org_id = view.kwargs.get("org_id")  # uuid in the URL

        if not api_key or not org_id or OrganizationAPIKey is None:
            return False

        try:
            # get_from_key verifies & returns the API key instance without exposing the secret
            key_obj = OrganizationAPIKey.objects.get_from_key(api_key)
            return str(key_obj.organization_id) == str(org_id)
        except Exception:
            return False


# --------------------------
# Helpers
# --------------------------

def _get_org(org_id) -> Organization:
    return get_object_or_404(Organization, id=org_id)


# --------------------------
# Ledgers
# --------------------------

@extend_schema(
    tags=["Tally / TCP"],
    request=TallyLedgerPayloadSerializer,
    responses={201: LedgerSerializer(many=True)},
)
class LedgerViewSet(viewsets.ViewSet):
    """
    POST /tally/org/{org_id}/ledgers/
    Body:
      {
        "LEDGER": [ { ...Tally ledger row... }, ... ]
      }
    """

    permission_classes = [HasOrgAPIKey]

    def create(self, request, org_id=None):
        _ = _get_org(org_id)  # ensure org exists
        serializer = TallyLedgerPayloadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ledger_data = serializer.validated_data["LEDGER"]

        created = []
        try:
            with transaction.atomic():
                for row in ledger_data:
                    parent_name = (row.get("Parent") or "").strip()
                    parent_ledger, _ = ParentLedger.objects.get_or_create(
                        organization_id=org_id,
                        parent=parent_name or None,
                    )
                    obj = Ledger(
                        organization_id=org_id,
                        master_id=row.get("Master_Id"),
                        alter_id=row.get("Alter_Id"),
                        name=row.get("Name"),
                        parent=parent_ledger,
                        alias=row.get("ALIAS"),
                        opening_balance=row.get("OpeningBalance", "0"),
                        gst_in=row.get("GSTIN"),
                        company=row.get("Company"),
                    )
                    created.append(obj)
                Ledger.objects.bulk_create(created)
        except Exception as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        # Return serialized created rows (re-query to include FKs)
        created_ids = [o.id for o in created]
        out_qs = Ledger.objects.filter(id__in=created_ids).select_related("parent")
        return Response(LedgerSerializer(out_qs, many=True).data, status=status.HTTP_201_CREATED)


# --------------------------
# Master (product master dump)
# --------------------------

@extend_schema(
    tags=["Tally / TCP"],
    request=MasterPayloadSerializer,
    responses={200: None},
)
class MasterAPIView(APIView):
    """
    POST /tally/org/{org_id}/master/
    We simply persist the raw body to disk for now (as per legacy behavior).
    """
    permission_classes = [HasOrgAPIKey]

    def post(self, request, org_id, *args, **kwargs):
        _ = _get_org(org_id)

        raw = request.body.decode("utf-8", errors="ignore")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        save_dir = getattr(settings, "TALLY_TCP_LOG_DIR", "incoming_logs")
        os.makedirs(save_dir, exist_ok=True)
        path = os.path.join(save_dir, f"incoming_data_{org_id}_{ts}.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(raw)
        return Response({"message": f"Incoming data saved.", "file": path}, status=status.HTTP_200_OK)


# --------------------------
# Expense (journal) -> GET synced
# --------------------------

@extend_schema(
    tags=["Tally / TCP"],
    responses={200: ExpenseSyncedResponseSerializer},
)
class TallyExpenseApi(APIView):
    """
    GET /tally/org/{org_id}/expense-bills/
      -> returns grouped DR/CR entries for all 'Synced' expense items
    POST /tally/org/{org_id}/expense-bills/
      -> accept payload (placeholder validation); can be extended to persist
    """
    permission_classes = [HasOrgAPIKey]

    def get(self, request, org_id, *args, **kwargs):
        _ = _get_org(org_id)

        bills = (
            TallyExpenseAnalyzedBill.objects
            .filter(selectBill__status="Synced", organization_id=org_id)
            .select_related("vendor", "selectBill")
            .prefetch_related("products")
        )

        grouped = defaultdict(lambda: {
            "id": None,
            "voucher": None,
            "bill_no": None,
            "bill_date": None,
            "total": 0.0,
            "name": "",
            "company": "",
            "gst_in": "",
            "DR_LEDGER": [],
            "CR_LEDGER": [],
            "note": "",
            "created_at": None,
        })

        for exp in bills:
            voucher = exp.voucher or "N/A"
            g = grouped[voucher]
            if not g["id"]:
                g.update({
                    "id": exp.id,
                    "voucher": voucher,
                    "bill_no": exp.bill_no or "N/A",
                    "bill_date": exp.bill_date,
                    "total": float(exp.total or 0.0) if isinstance(exp.total, (int, float, str)) else 0.0,
                    "name": exp.vendor.name if exp.vendor else "No Vendor",
                    "company": exp.vendor.company if exp.vendor else "No Company",
                    "gst_in": exp.vendor.gst_in if exp.vendor else "No GST",
                    "note": exp.note or "",
                    "created_at": localtime(exp.created_at).strftime("%Y-%m-%d %H:%M:%S") if exp.created_at else None,
                })

            for p in exp.products.all():
                ledger_name = p.chart_of_accounts.name if p.chart_of_accounts else "Unknown Ledger"
                amount = float(p.amount or 0.0)
                entry = {"LEDGERNAME": ledger_name, "AMOUNT": amount}
                if (p.debit_or_credit or "").lower() == "debit":
                    g["DR_LEDGER"].append(entry)
                else:
                    g["CR_LEDGER"].append(entry)

        data = {"data": list(grouped.values())}
        return Response(ExpenseSyncedResponseSerializer(data).data, status=status.HTTP_200_OK)

    @extend_schema(request=MasterPayloadSerializer, responses={200: None})
    def post(self, request, org_id, *args, **kwargs):
        _ = _get_org(org_id)
        payload = request.data  # accept, validate minimal fields if needed
        # Add your persistence or routing to internal queue here
        return Response({"message": "Payload received successfully"}, status=status.HTTP_200_OK)


# --------------------------
# Vendor Bills -> GET synced
# --------------------------

@extend_schema(
    tags=["Tally / TCP"],
    responses={200: VendorSyncedResponseSerializer},
)
class TallyVendor(APIView):
    """
    GET /tally/org/{org_id}/vendor-bills/
      -> returns synced vendor bills with transactions
    POST /tally/org/{org_id}/vendor-bills/
      -> accept payload for vendor-bill create/update (placeholder)
    """
    permission_classes = [HasOrgAPIKey]

    def get(self, request, org_id, *args, **kwargs):
        _ = _get_org(org_id)

        vendors = (
            TallyVendorAnalyzedBill.objects
            .filter(selectBill__status="Synced", organization_id=org_id)
            .select_related("vendor", "selectBill")
            .prefetch_related("products")
        )

        result = []
        for v in vendors:
            result.append({
                "id": v.id,
                "bill_no": v.bill_no,
                "bill_date": v.bill_date,
                "total": float(v.total or 0),
                "igst": float(v.igst or 0),
                "cgst": float(v.cgst or 0),
                "sgst": float(v.sgst or 0),
                "vendor": {
                    "name": v.vendor.name if v.vendor else "No Vendor",
                    "company": v.vendor.company if v.vendor else "No Company",
                    "gst_in": v.vendor.gst_in if v.vendor else "No GST",
                },
                # for external consumers, expose org id as 'customer_id'
                "customer_id": v.organization_id,
                "transactions": [
                    {
                        "id": t.id,
                        "item_name": t.item_name,
                        "item_details": t.item_details,
                        "price": float(t.price or 0),
                        "quantity": int(t.quantity or 0),
                        "amount": float(t.amount or 0),
                        "gst": t.product_gst,
                        "igst": float(t.igst or 0),
                        "cgst": float(t.cgst or 0),
                        "sgst": float(t.sgst or 0),
                    }
                    for t in v.products.all()
                ],
            })

        data = {"data": result}
        return Response(VendorSyncedResponseSerializer(data).data, status=status.HTTP_200_OK)

    @extend_schema(request=MasterPayloadSerializer, responses={200: None})
    def post(self, request, org_id, *args, **kwargs):
        _ = _get_org(org_id)
        payload = request.data
        # TODO: add your create/update logic or enqueue
        # Minimal validation example:
        bill_details = (payload or {}).get("bill_details") or {}
        if not bill_details.get("bill_number"):
            return Response({"message": "Missing required field: bill_number"}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"message": "Payload received successfully"}, status=status.HTTP_200_OK)

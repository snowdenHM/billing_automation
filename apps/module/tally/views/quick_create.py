"""
Quick-create endpoints for Tally masters — used by the "+ Add New"
buttons in the bill detail view and standalone Ledgers / Items pages.

All three endpoints follow the same shape:

  * Accept a small JSON body with the human-visible fields.
  * Create the record locally with ``source=billmunshi``, ``tally_synced=False``.
  * Return the fully-formed row so the frontend can populate its
    dropdown immediately.

The record then enters the pending-sync queue — Tally's TDL polls
``/masters/pending_sync/`` and creates it on its side, then calls
``/masters/mark_synced/`` back with the assigned ``master_id``. See
``docs/tally-master-sync.md`` for the full contract.
"""
from __future__ import annotations

import logging

from django.db import transaction
from drf_spectacular.utils import extend_schema, OpenApiResponse
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.permissions import IsOrgAdmin
from apps.common.utils import get_organization_from_request

from ..models import Ledger, ParentLedger, StockItem, SyncSource
from ..serializers import LedgerSerializer, StockItemSerializer

logger = logging.getLogger(__name__)


def _org_not_found_response(org_id):
    return Response(
        {
            "error": "Organization Access Denied",
            "message": f"Organization with ID {org_id} not found or you do not have access to it.",
            "error_code": "ORG_NOT_FOUND",
        },
        status=status.HTTP_404_NOT_FOUND,
    )


def _resolve_parent_ledger(*, organization, parent_id=None, parent_name=None):
    """Look up a ParentLedger by ID first, else by (case-insensitive) name.

    If both are None or the name is blank, defaults to ``"Uncategorized"``
    to keep the row valid — Tally's TDL will move it under a proper
    group when the master is created there.
    """
    if parent_id:
        try:
            return ParentLedger.objects.get(id=parent_id, organization=organization)
        except ParentLedger.DoesNotExist:
            pass

    name = (parent_name or "").strip() or "Uncategorized"
    parent, _created = ParentLedger.objects.get_or_create(
        organization=organization,
        parent__iexact=name,
        defaults={
            "parent": name,
            # A parent auto-created by a BM quick-add is itself a BM
            # record until Tally confirms it — mirror the child.
            "source": SyncSource.BILLMUNSHI,
            "tally_synced": False,
        },
    )
    return parent


# ---------------------------------------------------------------------------
# Vendor (creates a Ledger row under the vendor parent group)
# ---------------------------------------------------------------------------

@extend_schema(
    summary="Quick-create Vendor ledger",
    description=(
        "Creates a Ledger row with source=billmunshi. Vendor GSTIN is "
        "stored in the ``gst_in`` field. Parent defaults to "
        "'Sundry Creditors' unless overridden."
    ),
    request={
        "application/json": {
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {"type": "string"},
                "gst_in": {"type": "string"},
                "parent_ledger_id": {"type": "string", "format": "uuid"},
                "parent_ledger_name": {"type": "string"},
                "alias": {"type": "string"},
                "opening_balance": {"type": "number"},
            },
        },
    },
    responses={201: LedgerSerializer},
    tags=["Tally · Quick Create"],
)
@api_view(["POST"])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def quick_create_vendor(request, org_id):
    organization = get_organization_from_request(request, org_id)
    if not organization:
        return _org_not_found_response(org_id)

    data = request.data or {}
    name = (data.get("name") or "").strip()
    if not name:
        return Response(
            {"error": "MISSING_NAME", "message": "Vendor name is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Reject duplicates in the same org — Tally would rename them
    # awkwardly otherwise ("XYZ (2)").
    if Ledger.objects.filter(organization=organization, name__iexact=name).exists():
        return Response(
            {
                "error": "DUPLICATE_LEDGER",
                "message": f"A ledger named '{name}' already exists in this organization.",
            },
            status=status.HTTP_409_CONFLICT,
        )

    parent = _resolve_parent_ledger(
        organization=organization,
        parent_id=data.get("parent_ledger_id"),
        parent_name=data.get("parent_ledger_name") or "Sundry Creditors",
    )

    with transaction.atomic():
        ledger = Ledger.objects.create(
            organization=organization,
            name=name,
            parent=parent,
            gst_in=(data.get("gst_in") or "").strip() or None,
            alias=(data.get("alias") or "").strip() or None,
            opening_balance=data.get("opening_balance") or 0,
            source=SyncSource.BILLMUNSHI,
            tally_synced=False,
        )

    serializer = LedgerSerializer(ledger, context={"request": request})
    return Response(serializer.data, status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------------------
# Ledger (Purchase / Expense — anything that isn't a vendor)
# ---------------------------------------------------------------------------

@extend_schema(
    summary="Quick-create Purchase/Expense ledger",
    description=(
        "Creates a general-purpose Ledger. Same as vendor create, but "
        "no GSTIN field and a different default parent."
    ),
    request={
        "application/json": {
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {"type": "string"},
                "parent_ledger_id": {"type": "string", "format": "uuid"},
                "parent_ledger_name": {"type": "string"},
                "alias": {"type": "string"},
                "opening_balance": {"type": "number"},
            },
        },
    },
    responses={201: LedgerSerializer},
    tags=["Tally · Quick Create"],
)
@api_view(["POST"])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def quick_create_ledger(request, org_id):
    organization = get_organization_from_request(request, org_id)
    if not organization:
        return _org_not_found_response(org_id)

    data = request.data or {}
    name = (data.get("name") or "").strip()
    if not name:
        return Response(
            {"error": "MISSING_NAME", "message": "Ledger name is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if Ledger.objects.filter(organization=organization, name__iexact=name).exists():
        return Response(
            {
                "error": "DUPLICATE_LEDGER",
                "message": f"A ledger named '{name}' already exists in this organization.",
            },
            status=status.HTTP_409_CONFLICT,
        )

    # Purchase/expense ledgers commonly sit under "Indirect Expenses" or
    # "Purchase Accounts"; keep the default neutral if unspecified.
    parent = _resolve_parent_ledger(
        organization=organization,
        parent_id=data.get("parent_ledger_id"),
        parent_name=data.get("parent_ledger_name") or "Indirect Expenses",
    )

    with transaction.atomic():
        ledger = Ledger.objects.create(
            organization=organization,
            name=name,
            parent=parent,
            alias=(data.get("alias") or "").strip() or None,
            opening_balance=data.get("opening_balance") or 0,
            source=SyncSource.BILLMUNSHI,
            tally_synced=False,
        )

    serializer = LedgerSerializer(ledger, context={"request": request})
    return Response(serializer.data, status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------------------
# Item (Stock Item / Inventory)
# ---------------------------------------------------------------------------

@extend_schema(
    summary="Quick-create Stock Item (inventory)",
    description=(
        "Creates a StockItem row with source=billmunshi. Tally will "
        "use ``gst_rate`` + ``hsn_code`` to seed the inventory master "
        "when the record is synced there."
    ),
    request={
        "application/json": {
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {"type": "string"},
                "unit": {"type": "string"},
                "gst_rate": {"type": "string", "example": "18%"},
                "hsn_code": {"type": "string"},
                "parent": {"type": "string", "example": "Primary"},
                "alias": {"type": "string"},
                "item_code": {"type": "string"},
            },
        },
    },
    responses={201: StockItemSerializer},
    tags=["Tally · Quick Create"],
)
@api_view(["POST"])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def quick_create_item(request, org_id):
    organization = get_organization_from_request(request, org_id)
    if not organization:
        return _org_not_found_response(org_id)

    data = request.data or {}
    name = (data.get("name") or "").strip()
    if not name:
        return Response(
            {"error": "MISSING_NAME", "message": "Item name is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if StockItem.objects.filter(organization=organization, name__iexact=name).exists():
        return Response(
            {
                "error": "DUPLICATE_ITEM",
                "message": f"A stock item named '{name}' already exists in this organization.",
            },
            status=status.HTTP_409_CONFLICT,
        )

    with transaction.atomic():
        item = StockItem.objects.create(
            organization=organization,
            name=name,
            unit=(data.get("unit") or "").strip() or None,
            gst_rate=(data.get("gst_rate") or "").strip() or None,
            hsn_code=(data.get("hsn_code") or "").strip() or None,
            parent=(data.get("parent") or "Primary").strip(),
            alias=(data.get("alias") or "").strip() or None,
            item_code=(data.get("item_code") or "").strip() or None,
            source=SyncSource.BILLMUNSHI,
            tally_synced=False,
        )

    serializer = StockItemSerializer(item, context={"request": request})
    return Response(serializer.data, status=status.HTTP_201_CREATED)

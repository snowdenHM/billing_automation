"""
Master-sync contract between Bill Munshi and the Tally TCP/TDL.

Two endpoints, mirroring the existing bill-sync polling pattern:

  * ``GET /masters/pending_sync/``
    Tally TCP polls this. Returns every master row with
    ``source=billmunshi`` and ``tally_synced=False`` — grouped by type
    (parent_ledgers, ledgers, items). The TDL walks the list, creates
    each on the Tally side, captures the newly-assigned MASTER_ID, then
    calls back into ``mark_synced``.

  * ``POST /masters/mark_synced/``
    Tally callback. Body is a batch of ``{type, id, status, master_id?, message?}``
    dicts. Backend flips ``tally_synced=True`` on success, records the
    MASTER_ID from Tally, and stores an error message on failure so the
    UI can surface it.

Auth: both endpoints accept either a bearer token (an operator user's
JWT — useful for manual retries) OR the org's issued API key (what the
TDL uses in production). No public exposure.
"""
from __future__ import annotations

import logging

from django.db import transaction
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.common.permissions import OrganizationAPIKeyOrBearerToken
from apps.common.utils import get_organization_from_request

from ..models import Ledger, ParentLedger, StockItem, SyncSource

logger = logging.getLogger(__name__)


_TYPE_MODEL_MAP = {
    "parent_ledger": ParentLedger,
    "ledger": Ledger,
    "item": StockItem,
}


def _org_not_found_response(org_id):
    return Response(
        {
            "error": "Organization Access Denied",
            "message": f"Organization with ID {org_id} not found or you do not have access to it.",
            "error_code": "ORG_NOT_FOUND",
        },
        status=status.HTTP_404_NOT_FOUND,
    )


# ---------------------------------------------------------------------------
# Pending-sync queue for Tally to consume
# ---------------------------------------------------------------------------

@extend_schema(
    summary="Fetch pending masters awaiting Tally sync",
    description=(
        "Returns every ParentLedger / Ledger / StockItem row created "
        "via the Bill Munshi quick-add flow that hasn't yet been "
        "confirmed on the Tally side. Tally's TDL should poll this "
        "on a schedule (e.g. every 30s) and process the returned batch."
    ),
    tags=["Tally · Master Sync"],
)
@api_view(["GET"])
@permission_classes([OrganizationAPIKeyOrBearerToken])
def masters_pending_sync(request, org_id):
    organization = get_organization_from_request(request, org_id)
    if not organization:
        return _org_not_found_response(org_id)

    pending_parents = ParentLedger.objects.filter(
        organization=organization,
        source=SyncSource.BILLMUNSHI,
        tally_synced=False,
    ).order_by("created_at")

    pending_ledgers = Ledger.objects.filter(
        organization=organization,
        source=SyncSource.BILLMUNSHI,
        tally_synced=False,
    ).select_related("parent").order_by("created_at")

    pending_items = StockItem.objects.filter(
        organization=organization,
        source=SyncSource.BILLMUNSHI,
        tally_synced=False,
    ).order_by("created_at")

    return Response({
        "organization": {"id": str(organization.id), "name": organization.name},
        "parent_ledgers": [
            {
                "id": str(p.id),
                "name": p.parent,
                "created_at": p.created_at.isoformat() if p.created_at else None,
            }
            for p in pending_parents
        ],
        "ledgers": [
            {
                "id": str(l.id),
                "name": l.name,
                "parent": l.parent.parent if l.parent else None,
                "alias": l.alias or "",
                "gst_in": l.gst_in or "",
                "opening_balance": (
                    float(l.opening_balance) if l.opening_balance is not None else 0.0
                ),
                "created_at": l.created_at.isoformat() if l.created_at else None,
            }
            for l in pending_ledgers
        ],
        "items": [
            {
                "id": str(i.id),
                "name": i.name,
                "unit": i.unit or "",
                "gst_rate": i.gst_rate or "",
                "hsn_code": i.hsn_code or "",
                "parent": i.parent or "Primary",
                "alias": i.alias or "",
                "item_code": i.item_code or "",
                "created_at": i.created_at.isoformat() if i.created_at else None,
            }
            for i in pending_items
        ],
        "counts": {
            "parent_ledgers": pending_parents.count(),
            "ledgers": pending_ledgers.count(),
            "items": pending_items.count(),
        },
    })


# ---------------------------------------------------------------------------
# Callback: Tally reports the result of creating each master
# ---------------------------------------------------------------------------

@extend_schema(
    summary="Callback: mark masters as synced (or failed) after Tally processes them",
    description=(
        "Body shape:\n\n"
        "```json\n"
        "{\"results\": [\n"
        "  {\"type\": \"ledger\", \"id\": \"<uuid>\", \"status\": \"ok\",\n"
        "   \"master_id\": \"12345\", \"alter_id\": \"1\"},\n"
        "  {\"type\": \"item\",   \"id\": \"<uuid>\", \"status\": \"error\",\n"
        "   \"message\": \"Duplicate name in Tally\"}\n"
        "]}\n"
        "```\n\n"
        "``type`` is one of ``parent_ledger``, ``ledger``, ``item``. "
        "``status`` must be ``ok`` or ``error``. On ``ok`` we flip the "
        "row to ``tally_synced=True`` and store the returned "
        "``master_id`` / ``alter_id``. On ``error`` we keep the row "
        "unsynced and save the message to ``tally_sync_message`` for "
        "the UI to surface."
    ),
    tags=["Tally · Master Sync"],
)
@api_view(["POST"])
@permission_classes([OrganizationAPIKeyOrBearerToken])
def masters_mark_synced(request, org_id):
    organization = get_organization_from_request(request, org_id)
    if not organization:
        return _org_not_found_response(org_id)

    results = (request.data or {}).get("results")
    if not isinstance(results, list) or not results:
        return Response(
            {
                "error": "INVALID_PAYLOAD",
                "message": "Body must be {\"results\": [...]} with at least one entry.",
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    updated = {"ok": 0, "error": 0, "not_found": 0}

    with transaction.atomic():
        for entry in results:
            rec_type = (entry.get("type") or "").strip().lower()
            rec_id = entry.get("id")
            status_val = (entry.get("status") or "").strip().lower()

            model = _TYPE_MODEL_MAP.get(rec_type)
            if model is None or not rec_id:
                updated["not_found"] += 1
                logger.warning("mark_synced: unknown type/id %r/%r", rec_type, rec_id)
                continue

            try:
                obj = model.objects.get(id=rec_id, organization=organization)
            except model.DoesNotExist:
                updated["not_found"] += 1
                logger.warning(
                    "mark_synced: %s id=%s not found in org %s",
                    rec_type, rec_id, organization.id,
                )
                continue

            if status_val == "ok":
                obj.tally_synced = True
                obj.tally_sync_message = entry.get("message") or ""
                # Persist the Tally-side identifiers so future lookups
                # (bulk-import, sync-status reconciliation) can match.
                if hasattr(obj, "master_id") and entry.get("master_id"):
                    obj.master_id = str(entry["master_id"])
                if hasattr(obj, "alter_id") and entry.get("alter_id"):
                    obj.alter_id = str(entry["alter_id"])
                obj.save()
                updated["ok"] += 1
            elif status_val == "error":
                obj.tally_synced = False
                obj.tally_sync_message = entry.get("message") or "Unknown error from Tally"
                obj.save(update_fields=["tally_sync_message"])
                updated["error"] += 1
            else:
                updated["not_found"] += 1
                logger.warning(
                    "mark_synced: unknown status %r for %s id=%s",
                    status_val, rec_type, rec_id,
                )

    return Response({
        "summary": updated,
        "message": (
            f"Processed {sum(updated.values())} result(s): "
            f"{updated['ok']} ok, {updated['error']} error, "
            f"{updated['not_found']} unknown."
        ),
    })

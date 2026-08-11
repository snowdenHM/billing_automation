"""
Pre-sync guard for Tally bills.

Before a bill's XML is handed to the Tally TCP, every Ledger / StockItem
it references must already exist on the Tally side — otherwise Tally
will reject the voucher with "Ledger not found" and the bill will be
stuck in a half-verified state.

This module walks the analysed bill's FK graph, collects every
referenced master that still has ``source=billmunshi`` and
``tally_synced=False``, and returns them as a flat list. Callers use
the list to:

  * Return a friendly ``409 WAITING_FOR_MASTERS`` response instead of
    firing the sync (so the UI can show "3 masters still importing…").
  * Log which pieces are holding things up.

Zero XML changes — this is a pure orchestration layer sitting in front
of the existing sync endpoints.
"""
from __future__ import annotations

from ..models import Ledger


_LEDGER_FIELDS_ON_ANALYZED_BILL = (
    "vendor",
    "igst_taxes",
    "cgst_taxes",
    "sgst_taxes",
    "tds_taxes",
    "other_adjustment_taxes",
    "round_off_taxes",
    # Vendor bill (Purchase Voucher) also carries these — a BM-created
    # ledger picked here was previously bypassing the guard, letting
    # sync fire with a ledger Tally doesn't know about yet.
    "discount_taxes",
    "cess_taxes",
    "freight_taxes",
)


def _safe_getattr(obj, attr):
    """Return getattr(obj, attr) or None. Some models don't have every field
    (expense-side models lack discount/cess/freight), so a plain getattr
    with default None keeps the walker generic across bill types.
    """
    try:
        return getattr(obj, attr, None)
    except Exception:
        return None


def _pending(ledger) -> bool:
    """A ledger is pending iff it was BM-created and hasn't been Tally-synced."""
    return bool(
        ledger
        and getattr(ledger, "source", None) == "billmunshi"
        and not getattr(ledger, "tally_synced", True)
    )


def _describe(ledger, role: str) -> dict:
    return {
        "type": "ledger",
        "id": str(ledger.id),
        "name": ledger.name,
        "parent": ledger.parent.parent if ledger.parent else None,
        "role": role,
        "message": ledger.tally_sync_message or "Waiting for Tally to create this ledger.",
    }


def find_pending_masters(analyzed_bill) -> list[dict]:
    """Return every unsynced BM-created master referenced by the bill.

    Works for all three Tally analyzed-bill shapes (vendor / expense /
    payment) since they share the same field names for bill-level tax
    ledgers plus a ``.products`` reverse manager whose entries carry a
    ``chart_of_accounts`` (or ``taxes``) FK.
    """
    if analyzed_bill is None:
        return []

    pending: list[dict] = []
    seen_ids: set[str] = set()
    # Track every referenced ledger — pending or not — so the parent
    # walk below can inspect all of them, not just those already pending.
    # Previous behaviour missed a Tally-synced child whose parent was
    # still BM-pending, so sync fired XML Tally rejected with
    # "Parent not found".
    referenced: list[tuple[object, str]] = []

    def _push(ledger, role):
        if not ledger:
            return
        referenced.append((ledger, role))
        if str(ledger.id) in seen_ids:
            return
        if _pending(ledger):
            seen_ids.add(str(ledger.id))
            pending.append(_describe(ledger, role))

    # Bill-level ledger references
    for field in _LEDGER_FIELDS_ON_ANALYZED_BILL:
        _push(_safe_getattr(analyzed_bill, field), role=field)

    # Product-level references (vendor bills carry both chart_of_accounts
    # and taxes; expense/payment bills carry only chart_of_accounts).
    try:
        products = analyzed_bill.products.all()
    except Exception:
        products = []
    for product in products:
        _push(getattr(product, "chart_of_accounts", None), role="product_chart_of_accounts")
        _push(getattr(product, "taxes", None), role="product_taxes")

    # Walk every referenced ledger's parent — a child can be Tally-synced
    # while its parent is still BM-pending.
    for ledger, role in referenced:
        try:
            parent = getattr(ledger, "parent", None)
            if not parent:
                continue
            if getattr(parent, "source", None) != "billmunshi":
                continue
            if getattr(parent, "tally_synced", True):
                continue
            key = f"parent-{parent.id}"
            if key in seen_ids:
                continue
            seen_ids.add(key)
            pending.append({
                "type": "parent_ledger",
                "id": str(parent.id),
                "name": parent.parent,
                "role": "parent_of_" + role,
                "message": parent.tally_sync_message or "Waiting for Tally to create this parent group.",
            })
        except Exception:
            continue

    return pending


def build_waiting_response_payload(pending: list[dict]) -> dict:
    """Uniform response body when a sync is blocked by pending masters.

    Kept in this module so the three sync entry points return the exact
    same JSON shape — the frontend has one code path to handle it.
    """
    return {
        "error": "WAITING_FOR_MASTERS",
        "message": (
            f"This bill references {len(pending)} master record"
            f"{'s' if len(pending) > 1 else ''} that Tally hasn't imported "
            "yet. Wait for the next Tally sync cycle (~30 seconds) and retry."
        ),
        "pending_masters": pending,
        "error_code": "MASTERS_PENDING",
    }

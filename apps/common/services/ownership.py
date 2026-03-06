"""
Shared bill-ownership validation.

Determines whether an AI-extracted bill belongs to a given organization by
comparing the party identified in the ``from`` or ``to`` JSON field against
the organization's GST number and name.

Every bill-view file delegates to the helpers here instead of maintaining
its own copy of the matching logic.
"""

import logging
import re

from apps.common.utils import (
    calculate_string_similarity,
    normalize_company_name_enhanced,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GST extraction helpers
# ---------------------------------------------------------------------------

_GST_PATTERNS = [
    r"GST\s*NO\.?\s*:?\s*([A-Z0-9]{15})",
    r"GSTIN/UIN\s*:?\s*([A-Z0-9]{15})",
    r"GSTIN\s*:?\s*([A-Z0-9]{15})",
    r"Tax\s*ID\s*:?\s*([A-Z0-9]{15})",
    r"UIN\s*:?\s*([A-Z0-9]{15})",
    r"Registration\s*No\.?\s*:?\s*([A-Z0-9]{15})",
    r"\b([A-Z0-9]{15})\b",
]


def extract_gst_from_address(address: str) -> str:
    """Try to find a 15-char GST number hidden in an address string."""
    if not address:
        return ""
    for pattern in _GST_PATTERNS:
        match = re.search(pattern, address.upper(), re.IGNORECASE)
        if match:
            gst = match.group(1).strip()
            logger.info("Found GST number in address: %s", gst)
            return gst
    return ""


# ---------------------------------------------------------------------------
# Internal comparison primitives
# ---------------------------------------------------------------------------

def _clean_gst(gst: str) -> str:
    return gst.replace(" ", "").replace("-", "").upper()


def _compare_gst(party_gst: str, org_gst: str) -> tuple[bool, bool]:
    """Return ``(exact_match, partial_match)`` for two GST strings."""
    if not party_gst or not org_gst:
        return False, False
    p, o = _clean_gst(party_gst), _clean_gst(org_gst)
    if p == o:
        return True, True
    if len(p) >= 10 and len(o) >= 10 and p[:10] == o[:10]:
        return False, True
    return False, False


def _name_similarity(party_name: str, org_name: str) -> float:
    """Similarity score (0–1) using the shared enhanced normaliser."""
    if not party_name or not org_name:
        return 0.0
    return calculate_string_similarity(
        normalize_company_name_enhanced(party_name),
        normalize_company_name_enhanced(org_name),
    )


def _base_details(party_name, party_gst, org_name, org_gst):
    return {
        "extracted_party_name": party_name,
        "extracted_party_gst": party_gst,
        "organization_name": org_name,
        "organization_gst": org_gst,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate_bill_ownership(
    json_data: dict,
    organization,
    *,
    check_field: str = "to",
    allow_empty: bool = False,
    bill_type: str = "bill",
) -> dict:
    """
    Validate whether a bill belongs to *organization*.

    Parameters
    ----------
    json_data : dict
        AI-extracted bill data containing ``from`` / ``to`` sub-dicts.
    organization :
        Organisation model instance (needs ``.name``, ``.gst_number``).
    check_field : ``'to'`` | ``'from'``
        Which JSON field holds the party that should match the org.
        Use ``'from'`` for tally vendor bills (vendor = org) and
        ``'to'`` for everything else (customer = org).
    allow_empty : bool
        When *True*, missing party info is treated as a pass (useful for
        expense receipts that lack a clear customer).
    bill_type : str
        Human-readable label used in log messages.

    Returns
    -------
    dict
        ``is_valid``, ``confidence`` (0–100), ``reason``, ``validation_details``
    """
    try:
        # ── extract party data ──────────────────────────────────────
        party_data = json_data.get(check_field, {})
        if isinstance(party_data, dict):
            party_name = party_data.get("name", "").strip()
            party_gst = party_data.get("gst_number", "").strip()
            party_address = party_data.get("address", "").strip()
        else:
            party_name = party_gst = party_address = ""

        if not party_gst and party_address:
            party_gst = extract_gst_from_address(party_address)

        org_name = getattr(organization, "name", "") or ""
        org_gst = getattr(organization, "gst_number", "") or ""

        logger.info(
            "Validating %s ownership — Party: %s (GST: %s) vs Org: %s (GST: %s)",
            bill_type, party_name, party_gst, org_name, org_gst,
        )

        # ── handle missing party info ──────────────────────────────
        if not party_name and not party_gst:
            if allow_empty:
                return {
                    "is_valid": True,
                    "confidence": 50,
                    "reason": (
                        f"✅ No {check_field} information found — "
                        f"assuming valid {bill_type} receipt"
                    ),
                    "validation_details": _base_details(
                        party_name, party_gst, org_name, org_gst,
                    ),
                }
            return {
                "is_valid": False,
                "confidence": 0,
                "reason": f"No {check_field} information found in {bill_type}",
                "validation_details": _base_details(
                    party_name, party_gst, org_name, org_gst,
                ),
            }

        # ── Priority 1: GST comparison ─────────────────────────────
        gst_score = 0
        exact, partial = _compare_gst(party_gst, org_gst)
        if exact:
            gst_score = 100
        elif partial:
            gst_score = 80

        # ── Priority 2: Name similarity ────────────────────────────
        name_score = 0
        name_sim = _name_similarity(party_name, org_name)
        if name_sim >= 0.9:
            name_score = 85
        elif name_sim >= 0.7:
            name_score = 65
        elif name_sim >= 0.5:
            name_score = 40

        # ── confidence (weighted) ──────────────────────────────────
        if gst_score > 0 and name_score > 0:
            confidence = int(gst_score * 0.7 + name_score * 0.3)
        elif name_score >= 80:
            confidence = name_score
        else:
            confidence = max(gst_score, name_score)

        is_valid = confidence >= 60

        # ── build human-readable reason ────────────────────────────
        reasons: list[str] = []
        if gst_score >= 100:
            reasons.append(f"GST match: {party_gst}")
        elif gst_score >= 80:
            reasons.append(f"Partial GST match: {party_gst}")
        if name_score >= 65:
            reasons.append(
                f"Name match ({int(name_sim * 100)}%): "
                f"{party_name} ≈ {org_name}"
            )
        elif name_score >= 40:
            reasons.append(
                f"Partial name match ({int(name_sim * 100)}%): {party_name}"
            )

        if is_valid:
            reason = f"✅ {'; '.join(reasons) or 'Match found'}"
        else:
            reason = (
                f"❌ {bill_type.capitalize()} NOT matched to organization. "
                f"Party: '{party_name}' (GST: '{party_gst}') ≠ "
                f"Org: '{org_name}' (GST: '{org_gst}')"
            )

        return {
            "is_valid": is_valid,
            "confidence": confidence,
            "reason": reason,
            "validation_details": {
                **_base_details(party_name, party_gst, org_name, org_gst),
                "name_match_score": name_score,
                "gst_match_score": gst_score,
                "name_similarity": round(name_sim, 3),
            },
        }

    except Exception as e:
        logger.error("Error validating %s ownership: %s", bill_type, e)
        return {
            "is_valid": False,
            "confidence": 0,
            "reason": f"Validation error: {str(e)}",
            "validation_details": {"error": str(e)},
        }


def validate_bill_ownership_simple(
    json_data: dict,
    organization,
    **kwargs,
) -> tuple[bool, str]:
    """Backward-compatible wrapper returning ``(bool, reason_str)``."""
    result = validate_bill_ownership(json_data, organization, **kwargs)
    return result["is_valid"], result["reason"]

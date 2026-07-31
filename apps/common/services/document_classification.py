"""Decide whether an uploaded file is actually a bill before storing it.

Uploads previously became a Draft bill unconditionally: any PDF — a QA
checklist, a contract, a screenshot — was accepted, given a bill number, and
queued for analysis. The user only discovered the mistake once the analysis
came back empty and the verification panel demanded a vendor, a chart of
accounts and taxes for a document that never had any.

This module runs a single cheap vision call *before* the row is created, so a
non-bill is refused at the door with an explanation instead of turning into a
draft somebody has to hunt down and delete.

Two deliberate design choices:

* **Fails open.** If OpenAI is unreachable, slow, or returns something
  unparseable, the upload proceeds. A screening step must never become a
  single point of failure for the product's primary action — the cost of
  letting through the occasional stray file is far lower than the cost of
  blocking every upload during an API incident.
* **Rejects only on confidence.** Borderline documents (faded scans, unusual
  layouts, delivery notes that double as invoices) are allowed through. The
  threshold is set so that only clearly-not-a-bill files are refused; a false
  rejection is much more annoying than a false acceptance, because the user
  has no way to override it.
"""

import logging

from django.conf import settings

logger = logging.getLogger(__name__)

# Document kinds the analyser recognises. Anything not in ACCEPTED is refused
# when the model is confident enough.
ACCEPTED_DOCUMENT_TYPES = {
    "invoice",
    "bill",
    "receipt",
    "credit_note",
    "debit_note",
    "purchase_order",
}

CLASSIFICATION_PROMPT = """You are screening a file that a user uploaded to an accounting system as a vendor bill.

Decide whether this document is a financial document that belongs in accounts payable.

Return ONLY a JSON object with these exact keys:
{
  "is_bill": true or false,
  "document_type": one of "invoice", "bill", "receipt", "credit_note", "debit_note", "purchase_order", "bank_statement", "contract", "report", "form", "letter", "presentation", "spreadsheet", "screenshot", "identity_document", "other",
  "confidence": a number from 0.0 to 1.0,
  "reason": a short sentence, max 20 words, explaining the decision in plain language for a non-technical user
}

Set "is_bill" to true when the document shows the hallmarks of a payable document: a supplier/vendor, line items or a description of goods or services, and monetary amounts or a total payable.

Set "is_bill" to false for anything else — test plans, checklists, contracts, manuals, reports, presentations, bank statements, ID documents, or blank/unreadable pages.

Use a confidence below 0.7 whenever you are genuinely unsure, for example a poor scan or an unfamiliar layout. Be decisive only when the document is clearly one or the other."""


def _config(name, default):
    return getattr(settings, name, default)


def _allow(reason, **extra):
    """Build an 'accept this upload' result."""
    result = {
        "is_bill": True,
        "document_type": "unknown",
        "confidence": 0.0,
        "reason": reason,
        "checked": False,
    }
    result.update(extra)
    return result


def classify_document(file_content, file_extension):
    """Return a verdict on whether ``file_content`` looks like a bill.

    Args:
        file_content: Raw file bytes.
        file_extension: Extension without the dot, e.g. ``"pdf"``.

    Returns a dict with:
        is_bill (bool):       False only when the file should be refused.
        document_type (str):  What the model thinks it is.
        confidence (float):   0.0-1.0.
        reason (str):         User-facing explanation.
        checked (bool):       False when screening was skipped or failed,
                              which is how callers can tell "looks like a
                              bill" apart from "we never managed to look".
    """
    if not _config("BILL_DOCUMENT_VALIDATION_ENABLED", True):
        return _allow("Document screening is disabled.")

    threshold = float(_config("BILL_DOCUMENT_VALIDATION_MIN_CONFIDENCE", 0.7))

    try:
        from apps.common.services.bill_analysis import (
            call_openai_analysis,
            prepare_bill_image_from_bytes,
        )

        image_b64, mime = prepare_bill_image_from_bytes(file_content, file_extension)
        raw = call_openai_analysis(
            image_b64,
            mime,
            CLASSIFICATION_PROMPT,
            # A verdict is a handful of tokens; no need for the analysis budget.
            max_tokens=200,
            temperature=0.0,
        )
    except Exception as exc:
        # Fail open — see module docstring.
        logger.warning(
            "Document screening unavailable (%s: %s) — allowing upload.",
            type(exc).__name__, exc,
        )
        return _allow("Could not screen this document; it was accepted.")

    if not isinstance(raw, dict):
        logger.warning("Document screening returned %s, not a dict — allowing.", type(raw))
        return _allow("Could not screen this document; it was accepted.")

    document_type = str(raw.get("document_type") or "other").strip().lower()
    reason = str(raw.get("reason") or "").strip()

    try:
        confidence = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    # Trust the explicit verdict, but require the document_type to agree —
    # the two disagreeing means the model was not really sure.
    says_bill = bool(raw.get("is_bill")) or document_type in ACCEPTED_DOCUMENT_TYPES

    if says_bill:
        return {
            "is_bill": True,
            "document_type": document_type,
            "confidence": confidence,
            "reason": reason or "Looks like a bill.",
            "checked": True,
        }

    if confidence < threshold:
        # Not confident enough to refuse — let it through rather than block a
        # legitimate bill the model simply could not read.
        logger.info(
            "Screening unsure about a %s (confidence %.2f < %.2f) — allowing.",
            document_type, confidence, threshold,
        )
        return {
            "is_bill": True,
            "document_type": document_type,
            "confidence": confidence,
            "reason": reason or "Unclear document; accepted for review.",
            "checked": True,
        }

    return {
        "is_bill": False,
        "document_type": document_type,
        "confidence": confidence,
        "reason": reason or "This file does not look like a bill or invoice.",
        "checked": True,
    }


def describe_rejection(file_name, verdict):
    """Build the user-facing payload for a refused upload."""
    readable = (verdict.get("document_type") or "other").replace("_", " ")
    return {
        "file": file_name,
        "reason": verdict.get("reason") or "This file does not look like a bill.",
        "detected_type": readable,
        "confidence": round(float(verdict.get("confidence") or 0.0), 2),
        "message": (
            f'"{file_name}" looks like a {readable}, not a bill, so it was not '
            f"uploaded. Please upload an invoice, bill or receipt."
        ),
    }

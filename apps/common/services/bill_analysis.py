# apps/common/services/bill_analysis.py
"""
Shared AI bill analysis service.

Consolidates the PDF/image → base64 → OpenAI → JSON pipeline that was
previously copy-pasted across all 5 bill view files.

Uses OpenAI vision (default ``gpt-4o``). The historical Ollama
fallback path has been removed — the free local model was never
production-viable and its config knobs added noise.
"""
import base64
import json
import logging
import os
import re
from io import BytesIO

from django.conf import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# OCR Engine Configuration — OpenAI only
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# OpenAI client (lazy singleton)
# ---------------------------------------------------------------------------
_openai_client = None


def _get_openai_client():
    """Return the singleton OpenAI client, creating it on first call."""
    global _openai_client
    if _openai_client is None:
        try:
            from openai import OpenAI
            api_key = getattr(settings, "OPENAI_API_KEY", None)
            if not api_key or api_key == "your_openai_api_key_here":
                raise ValueError("OpenAI API key not configured in settings")
            _openai_client = OpenAI(
                api_key=api_key,
                timeout=getattr(settings, "OPENAI_REQUEST_TIMEOUT", 60.0),
                max_retries=getattr(settings, "OPENAI_MAX_RETRIES", 2),
            )
        except ImportError:
            raise RuntimeError("openai package is not installed")
    return _openai_client


# ---------------------------------------------------------------------------
# File → base64 conversion
# ---------------------------------------------------------------------------

def prepare_bill_image(file_path, file_name):
    """Read a bill file (PDF or image) and return ``(base64_str, mime_type)``.

    For PDFs the first page is rendered at 200 DPI, contrast/sharpness
    enhanced, and upscaled to a minimum of 1000 px on the shortest side.

    Returns a tuple ``(image_base64: str, mime_type: str)``.
    Raises ``Exception`` on failure.
    """
    file_name_lower = file_name.lower()

    if file_name_lower.endswith(".pdf"):
        return _prepare_pdf_image(file_path)
    else:
        return _prepare_raw_image(file_path, file_name_lower)


def _prepare_pdf_image(file_path):
    """Convert the first page of a PDF to an optimised JPEG base64 string."""
    from PIL import Image, ImageEnhance
    from pdf2image import convert_from_bytes

    with open(file_path, "rb") as f:
        pdf_bytes = f.read()

    if not pdf_bytes.startswith(b"%PDF"):
        raise ValueError("Invalid PDF file format")
    if len(pdf_bytes) < 100:
        raise ValueError("PDF file too small (possibly corrupted)")

    page_images = convert_from_bytes(pdf_bytes, first_page=1, last_page=1, dpi=200, fmt="jpeg")
    if not page_images:
        raise ValueError("No images generated from PDF")

    image = page_images[0]
    if image.mode != "RGB":
        image = image.convert("RGB")

    # Enhance for better OCR
    image = ImageEnhance.Contrast(image).enhance(1.2)
    image = ImageEnhance.Sharpness(image).enhance(1.1)

    # Ensure minimum size
    width, height = image.size
    if width < 1000 or height < 1000:
        scale = max(1000 / width, 1000 / height)
        image = image.resize((int(width * scale), int(height * scale)), Image.Resampling.LANCZOS)

    buf = BytesIO()
    image.save(buf, format="JPEG", quality=95)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("utf-8"), "image/jpeg"


def _prepare_pdf_image_from_bytes(pdf_bytes):
    """Like ``_prepare_pdf_image`` but accepts raw PDF bytes directly."""
    from PIL import Image, ImageEnhance
    from pdf2image import convert_from_bytes

    if not pdf_bytes.startswith(b"%PDF"):
        raise ValueError("Invalid PDF file format")
    if len(pdf_bytes) < 100:
        raise ValueError("PDF file too small (possibly corrupted)")

    page_images = convert_from_bytes(pdf_bytes, first_page=1, last_page=1, dpi=200, fmt="jpeg")
    if not page_images:
        raise ValueError("No images generated from PDF")

    image = page_images[0]
    if image.mode != "RGB":
        image = image.convert("RGB")

    image = ImageEnhance.Contrast(image).enhance(1.2)
    image = ImageEnhance.Sharpness(image).enhance(1.1)

    width, height = image.size
    if width < 1000 or height < 1000:
        scale = max(1000 / width, 1000 / height)
        image = image.resize((int(width * scale), int(height * scale)), Image.Resampling.LANCZOS)

    buf = BytesIO()
    image.save(buf, format="JPEG", quality=95)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("utf-8"), "image/jpeg"


def _prepare_raw_image(file_path, file_name_lower):
    """Read an image file and return its base64 encoding + MIME type."""
    with open(file_path, "rb") as f:
        content = f.read()

    mime_map = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
        ".webp": "image/webp",
    }
    mime_type = "image/jpeg"  # default
    for ext, mt in mime_map.items():
        if file_name_lower.endswith(ext):
            mime_type = mt
            break

    return base64.b64encode(content).decode("utf-8"), mime_type


# Shared MIME map
_MIME_MAP = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "gif": "image/gif", "bmp": "image/bmp", "webp": "image/webp",
}


def prepare_bill_image_from_bytes(file_content, file_extension):
    """Like ``prepare_bill_image`` but accepts raw bytes + extension string.

    Args:
        file_content: Raw file bytes.
        file_extension: Extension string (e.g. ``"pdf"``, ``"jpg"``). Leading dot is stripped.

    Returns ``(base64_str, mime_type)``.
    """
    ext = file_extension.lower().lstrip(".")
    if ext == "pdf":
        return _prepare_pdf_image_from_bytes(file_content)
    mime = _MIME_MAP.get(ext, "image/jpeg")
    return base64.b64encode(file_content).decode("utf-8"), mime


def analyze_bill_bytes(file_content, file_extension, prompt):
    """Full pipeline from raw bytes: bytes -> image -> OpenAI OCR -> validated JSON dict.

    Convenience wrapper for Zoho-style callers that receive ``(bytes, ext)``
    instead of ``(file_path, file_name)``.
    """
    image_b64, mime = prepare_bill_image_from_bytes(file_content, file_extension)
    json_data = call_ocr_analysis(image_b64, mime, prompt)
    return ensure_gst_fields(json_data)


# ---------------------------------------------------------------------------
# OpenAI call + JSON parsing
# ---------------------------------------------------------------------------

def call_ocr_analysis(image_base64, mime_type, prompt, model=None,
                      max_tokens=2000, temperature=0.1):
    """Unified OCR analysis via OpenAI vision.

    Historical: this used to fan out to Ollama with fallback semantics.
    That path was removed — see module docstring. The signature is
    preserved for backward compatibility with existing callers.

    Args:
        image_base64: Base64-encoded image
        mime_type: MIME type of the image
        prompt: Analysis prompt
        model: Optional OpenAI model override (default ``gpt-4o``)
        max_tokens: Maximum tokens for response
        temperature: Temperature for generation

    Returns:
        Parsed JSON dict from OCR analysis
    """
    logger.info("Running OCR analysis via OpenAI")
    return call_openai_analysis(
        image_base64, mime_type, prompt,
        model=model or "gpt-4o",
        max_tokens=max_tokens,
        temperature=temperature,
    )


def call_openai_analysis(image_base64, mime_type, prompt, model="gpt-4o",
                         max_tokens=2000, temperature=0.1):
    """Send an image to OpenAI for analysis and return the parsed JSON dict.

    Handles markdown code-block extraction and brace-extraction fallbacks.
    Raises ``Exception`` on total failure.
    """
    client = _get_openai_client()

    response = client.chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime_type};base64,{image_base64}",
                            "detail": "high",
                        },
                    },
                ],
            }
        ],
        max_tokens=max_tokens,
        temperature=temperature,
    )

    if not response.choices or not response.choices[0].message.content:
        raise ValueError("Empty response from OpenAI API")

    raw = response.choices[0].message.content.strip()
    return parse_openai_json_response(raw)


def parse_openai_json_response(raw):
    """Best-effort JSON extraction from an OpenAI text response.

    Tries: markdown stripping → direct parse → brace extraction → fallback dict.
    """
    # 0. Aggressive markdown code-block stripping
    # Some models wrap JSON in ```json\n...\n``` markers
    cleaned = raw.strip()
    
    # Try regex-based extraction first (handles various markdown formats)
    markdown_pattern = r'^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$'
    match = re.match(markdown_pattern, cleaned, re.DOTALL)
    if match:
        cleaned = match.group(1).strip()
        logger.debug(f"Stripped markdown wrapper, extracted JSON (first 200 chars): {cleaned[:200]}")
    
    # Also handle inline code blocks with just ``` (no json specifier)
    elif cleaned.startswith('```') and cleaned.endswith('```'):
        # Remove leading ``` and trailing ```
        cleaned = cleaned[3:-3].strip()
        # If it starts with 'json\n', remove that too
        if cleaned.startswith('json\n'):
            cleaned = cleaned[5:].strip()
        elif cleaned.startswith('json '):
            cleaned = cleaned[5:].strip()
        logger.debug(f"Stripped simple markdown wrapper, extracted JSON (first 200 chars): {cleaned[:200]}")
    
    # 1. Direct parse with cleaned content
    try:
        result = json.loads(cleaned)
        logger.debug("Successfully parsed JSON directly")
        return result
    except json.JSONDecodeError as e:
        logger.debug(f"Direct JSON parse failed: {e}")
        pass

    # 2. Brace extraction as last resort (in case there's text before/after JSON)
    if "{" in cleaned and "}" in cleaned:
        start = cleaned.find("{")
        end = cleaned.rfind("}") + 1
        if end > start:
            try:
                extracted = cleaned[start:end]
                result = json.loads(extracted)
                logger.debug("Successfully parsed JSON after brace extraction")
                return result
            except json.JSONDecodeError as e:
                logger.debug(f"Brace extraction JSON parse failed: {e}")
                pass

    # 3. Fallback
    logger.error("All JSON parsing attempts failed for OpenAI response")
    logger.error(f"Raw response (first 500 chars): {raw[:500]}")
    return {
        "error": "json_parse_failed",
        "raw_response": raw[:500] + "..." if len(raw) > 500 else raw,
        "fallback_data": {
            "invoiceNumber": "PARSE_ERROR",
            "dateIssued": "",
            "dueDate": "",
            "from": {"name": "VENDOR_PARSE_ERROR", "address": "", "gst_number": ""},
            "to": {"name": "", "address": "", "gst_number": ""},
            "totalAmount": 0,
            "lineItems": [],
        },
    }


def ensure_gst_fields(json_data):
    """Ensure ``gst_number`` exists in both ``from`` and ``to`` sections.

    Mutates *json_data* in-place and returns it.
    """
    if not isinstance(json_data, dict):
        return json_data

    data = json_data.get("fallback_data", json_data)

    for section in ("from", "to"):
        if section not in data or not isinstance(data[section], dict):
            data[section] = {"name": "", "address": "", "gst_number": ""}
        elif "gst_number" not in data[section]:
            data[section]["gst_number"] = ""

    return json_data


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

# GST extraction preamble shared by all bill types
_GST_EXTRACTION_PREAMBLE = """
    🚨 CRITICAL MANDATORY REQUIREMENT 🚨
    YOU MUST ALWAYS INCLUDE "gst_number" field in BOTH "from" and "to" sections.
    DO NOT OMIT THIS FIELD UNDER ANY CIRCUMSTANCES.
    If no GST number is found, use empty string "" but the field MUST be present.
    
    🔍 AGGRESSIVE GST NUMBER SEARCH:
    GST numbers in Indian invoices appear in various formats and locations:
    - GSTIN: 22AAAAA0000A1Z5 (15-character alphanumeric code)
    - GST No: 06AADCK7940H1ZG
    - Tax ID: 27AABCU9603R1ZX
    - Registration No: followed by GST number
    - Often embedded in addresses like "GST NO. 123456... State Name: Delhi, Code: 07"
    - May appear as "GSTIN/UIN :" followed by the number
    - Sometimes shown in headers, footers, or separate tax information sections
    - Can be in vendor section (from) and customer section (to)
    - Look for patterns like "06AADCK7940H1ZG", "22AAAAA0000A1Z5"
    - Check every line of text for 15-character alphanumeric codes
    - May be prefixed with "GST:", "GSTIN:", "Tax ID:", "REG NO:"
    
    🔎 SEARCH LOCATIONS (CHECK ALL):
    1. Company headers and letterheads
    2. Address blocks (often at the end of addresses)
    3. Tax information sections and tables
    4. Registration details sections
    5. Footer information
    6. Any line containing "GST", "GSTIN", "Tax ID", "UIN", "Registration", "REG"
    7. Business registration details
    8. Company information boxes
    9. Billing address sections
    10. Shipping address sections
    11. Invoice metadata sections
    12. Tax calculation tables
    
    📋 EXTRACTION REQUIREMENTS:
    1. Invoice/Bill Number (Invoice No, Bill No, Receipt No, etc.)
    2. Dates (Invoice Date, Bill Date, Due Date - convert to YYYY-MM-DD format)
    3. Vendor/Company details (from - who is billing)
    4. Customer details (to - who is being billed)
    5. Line items with descriptions, quantities, and prices
    6. Tax amounts (IGST, CGST, SGST - look for percentages and amounts)
    7. Total amount (Total, Grand Total, Amount Payable)
    8. GST NUMBERS - ABSOLUTELY MANDATORY FIELD
    
    ⚠️ CRITICAL RULES:
    - THE "gst_number" FIELD IS MANDATORY IN BOTH "from" AND "to" OBJECTS
    - If you cannot find a GST number, use empty string "" but DO NOT omit the field
    - Extract EXACT text as it appears on the document
    - For numbers, remove currency symbols (₹, Rs.) and commas
    - If any other field is not visible, use empty string "" or 0 for numbers
    - Look carefully at the entire document, including headers, footers, and margins
    - Pay special attention to tax sections which may be in tables or separate areas
    - GST numbers are typically 15-character codes - extract the full code
    - Check every text line for potential GST numbers
    - Look for format: 2 digits + 10 alphanumeric characters + 1 digit + 2 characters
    
    ⚠️ FINAL REMINDER: The "gst_number" field MUST be present in both "from" and "to" sections, even if empty.

    🔢 NUMERIC ACCURACY — CRITICAL FOR LARGE AMOUNTS (7+ DIGITS)
    Numbers on Indian invoices routinely reach 6, 7 or 8 digits
    (₹1,23,45,678 = ~₹1.2 crore). OCR frequently drops the leading
    digit of large amounts because the image crop or bounding box
    cuts it off. Every numeric field you extract MUST be verified.

    STRICT digit rules — apply to EVERY amount field
    (price, quantity, item amount, igst, cgst, sgst, tds,
    round_off, discount, cess, freight, total):

    1. Count the digits BEFORE the decimal / paise. If the printed
       value on the invoice is written with Indian grouping
       (₹1,23,45,678.90 or ₹5,82,338), count the digits between the
       commas — that is authoritative. A value written as "5,82,338"
       has exactly 6 digits (5 lakh 82 thousand 338), NOT 5 digits.
    2. When the invoice prints both a per-line amount AND a
       subtotal / grand total, ALWAYS verify that
       Σ(line.amount) + (igst + cgst + sgst) + (cess + discount
                       + freight) ≈ total to within ₹5. If it does
       not, RE-READ the digits of every offending field. Prefer a
       higher digit count when in doubt — a missed leading digit
       (₹5,82,338 read as ₹82,338) is the #1 OCR failure mode on
       this dataset.
    3. Never smooth or "round" a figure to look plausible. Extract
       the exact digits printed on the invoice. If the printed value
       and the arithmetic don't match, extract the printed value AS-IS
       and let the backend flag the drift — do not silently fix it.
    4. Currency prefixes and separators ("₹", "Rs.", "INR", ",",
       spaces) MUST be stripped from the returned JSON value —
       return the raw number only.
    5. For "Amount in Words" fields on the invoice ("Rupees Five
       Lakh Eighty-Two Thousand Three Hundred Thirty-Eight Only"),
       cross-check the words against the digit-count of your
       extracted total. If they disagree, TRUST THE WORDS — they are
       written by a human and are almost always correct — and
       re-read the digits.
    6. For any line item quantity, extract the exact printed value
       (integer or decimal). Fractional quantities like "1.5 kg" are
       real; do not truncate to 1.

    Return a single JSON object matching the schema below. Do NOT
    include commentary, markdown fences, or narration.
"""


def check_ocr_totals_sanity(relevant_data):
    """Cross-check Σ(items) + Σ(taxes) + adjustments vs the printed total.

    OCR frequently drops the leading digit of a 7-8 digit amount
    (client Corrections 14 + 24). If the extracted line-item total
    plus taxes drift far from the printed grand total, it almost
    always means a digit was misread — flag it so the operator can
    re-enter that field on the verify screen instead of syncing a
    wrong voucher into Tally.

    Returns a dict:
        {"ok": bool, "drift": float, "drift_pct": float, "message": str}
    """
    try:
        def _f(v):
            try:
                return float(v or 0)
            except (TypeError, ValueError):
                return 0.0

        items = relevant_data.get('items') or relevant_data.get('expenses') or []
        if not isinstance(items, list):
            items = []

        def _line_amount(row):
            if not isinstance(row, dict):
                return 0.0
            # vendor-bill shape: price × quantity
            price = _f(row.get('price'))
            qty = _f(row.get('quantity'))
            if price and qty:
                return price * qty
            # expense-bill shape: flat amount
            return _f(row.get('amount'))

        line_sum = sum(_line_amount(r) for r in items)
        tax_sum = (
            _f(relevant_data.get('igst'))
            + _f(relevant_data.get('cgst'))
            + _f(relevant_data.get('sgst'))
        )
        # Every adjustment is signed and simply added — a discount that
        # reduces the bill arrives as a negative amount.
        adjustments = (
            _f(relevant_data.get('cess'))
            + _f(relevant_data.get('discount'))
            + _f(relevant_data.get('freight'))
            + _f(relevant_data.get('round_off'))
        )
        computed = line_sum + tax_sum + adjustments
        printed = _f(relevant_data.get('total'))

        if not printed or not line_sum:
            return {"ok": True, "drift": 0.0, "drift_pct": 0.0, "message": ""}

        drift = abs(computed - printed)
        drift_pct = (drift / printed) * 100.0 if printed else 0.0

        # Threshold: >5% AND >₹100 drift is almost always a leading-digit
        # OCR miss on either an item, a tax field, or the total itself.
        if drift < 100 or drift_pct < 5.0:
            return {"ok": True, "drift": drift, "drift_pct": drift_pct, "message": ""}

        # Detect the most likely culprit: which single field, when its
        # magnitude is multiplied by 10, brings the totals into alignment?
        # A missed leading digit typically = printed_value × 10.
        suspects = []
        for label, val in (
            ("total", printed),
            ("igst", _f(relevant_data.get('igst'))),
            ("cgst", _f(relevant_data.get('cgst'))),
            ("sgst", _f(relevant_data.get('sgst'))),
        ):
            if val <= 0:
                continue
            # If val×10 aligns the totals, that field is a strong suspect.
            if abs(computed - (printed - val + val * 10)) < 1.0:
                suspects.append(label)
        for idx, r in enumerate(items):
            amt = _line_amount(r)
            if amt <= 0:
                continue
            if abs(computed - amt + amt * 10 - printed) < 1.0:
                suspects.append(f"item[{idx}]")

        msg = (
            f"OCR sanity check: computed ₹{computed:,.2f} vs printed ₹{printed:,.2f} "
            f"(drift ₹{drift:,.2f} = {drift_pct:.1f}%)."
        )
        if suspects:
            msg += f" Likely missed leading digit on: {', '.join(suspects)}."

        return {
            "ok": False,
            "drift": drift,
            "drift_pct": drift_pct,
            "message": msg,
            "suspects": suspects,
        }
    except Exception as e:
        logger.warning("check_ocr_totals_sanity failed: %s", e)
        return {"ok": True, "drift": 0.0, "drift_pct": 0.0, "message": ""}


def get_vendor_bill_prompt():
    """Return the OpenAI prompt for vendor bill analysis (items-based)."""
    return (
        "Analyze this Indian invoice/bill image very carefully and extract ALL visible information in JSON format.\n"
        + _GST_EXTRACTION_PREAMBLE
        + """
    🎯 MANDATORY JSON STRUCTURE:
    {
        "invoiceNumber": "Invoice/Bill number as shown on document",
        "dateIssued": "Invoice/Bill date in YYYY-MM-DD format",
        "dueDate": "Due date in YYYY-MM-DD format if mentioned, empty string if not",
        "from": {
            "name": "Vendor/Company name (who is sending the bill)",
            "address": "Complete vendor address",
            "gst_number": "Vendor's GST number or empty string if not found - FIELD IS MANDATORY"
        },
        "to": {
            "name": "Customer name (who is receiving the bill)",
            "address": "Complete customer address",
            "gst_number": "Customer's GST number or empty string if not found - FIELD IS MANDATORY"
        },
        "items": [
            {
                "description": "Item/Service description",
                "quantity": 0,
                "price": 0
            }
        ],
        "total": 0,
        "igst": 0,
        "cgst": 0,
        "sgst": 0,
        "round_off": 0,
        "discount": 0,
        "cess": 0,
        "freight": 0
    }

    round_off / discount / cess / freight EXTRACTION RULES:
      - If the invoice explicitly prints a line like
        "ROUND OFF VALUE (₹)", "Rounded Off", "R/O" — put that
        signed amount in ``round_off`` (may be negative).
      - If the invoice prints "Discount", "Trade Discount",
        "Less: Discount" — put the amount in ``discount`` as a SIGNED
        value: a discount that reduces the bill is NEGATIVE
        (e.g. "Less: Discount 82.00" → -82.00). Only use a positive
        value if the invoice genuinely adds the amount to the total.
      - "Cess", "GST Cess" → ``cess``. "Freight", "Shipping",
        "Delivery Charges" → ``freight``.
      - If the field is absent on the invoice, return 0. Do NOT
        derive round_off from a total-minus-tax subtraction —
        that's the backend's job. Only extract what is printed.
"""
    )


def get_expense_bill_prompt():
    """Return the OpenAI prompt for expense bill analysis (expenses-based)."""
    return (
        "Analyze this Indian expense invoice/bill image very carefully and extract ALL visible information in JSON format.\n"
        + _GST_EXTRACTION_PREAMBLE
        + """
    🎯 MANDATORY JSON STRUCTURE:
    {
        "invoiceNumber": "Invoice/Bill number as shown on document",
        "dateIssued": "Invoice/Bill date in YYYY-MM-DD format",
        "dueDate": "Due date in YYYY-MM-DD format if mentioned, empty string if not",
        "from": {
            "name": "Vendor/Company name (who is sending the bill)",
            "address": "Complete vendor address",
            "gst_number": "Vendor's GST number or empty string if not found - FIELD IS MANDATORY"
        },
        "to": {
            "name": "Customer name (who is receiving the bill)",
            "address": "Complete customer address",
            "gst_number": "Customer's GST number or empty string if not found - FIELD IS MANDATORY"
        },
        "expenses": [
            {
                "description": "Expense description",
                "category": "Expense category",
                "amount": 0
            }
        ],
        "total": 0,
        "igst": 0,
        "cgst": 0,
        "sgst": 0,
        "tds": 0
    }
"""
    )


def get_journal_bill_prompt():
    """Return the OpenAI prompt for journal bill analysis (journal entries)."""
    return (
        "Analyze this Indian journal entry/bill image very carefully and extract ALL visible information in JSON format.\n"
        + _GST_EXTRACTION_PREAMBLE
        + """
    🎯 MANDATORY JSON STRUCTURE:
    {
        "invoiceNumber": "Invoice/Bill number as shown on document",
        "dateIssued": "Invoice/Bill date in YYYY-MM-DD format",
        "dueDate": "Due date in YYYY-MM-DD format if mentioned, empty string if not",
        "from": {
            "name": "Vendor/Company name (who is sending the bill)",
            "address": "Complete vendor address",
            "gst_number": "Vendor's GST number or empty string if not found - FIELD IS MANDATORY"
        },
        "to": {
            "name": "Customer name (who is receiving the bill)",
            "address": "Complete customer address",
            "gst_number": "Customer's GST number or empty string if not found - FIELD IS MANDATORY"
        },
        "journal_entries": [
            {
                "account": "Account name",
                "description": "Description",
                "debit": 0,
                "credit": 0
            }
        ],
        "total": 0,
        "igst": 0,
        "cgst": 0,
        "sgst": 0
    }
"""
    )


# ---------------------------------------------------------------------------
# High-level convenience: file → analysed JSON
# ---------------------------------------------------------------------------

def analyze_bill_file(file_path, file_name, prompt):
    """Full pipeline: read file → image → OpenAI OCR → validated JSON dict.

    Args:
        file_path: Absolute path to the bill file.
        file_name: Original filename (used for extension detection).
        prompt: The prompt string to send to OCR engine.

    Returns:
        ``dict`` — the extracted JSON data (with GST fields ensured).
    Raises on failure.
    """
    image_b64, mime = prepare_bill_image(file_path, file_name)
    json_data = call_ocr_analysis(image_b64, mime, prompt)
    return ensure_gst_fields(json_data)

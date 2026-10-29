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


# Most bills are one or two pages. The cap stops a batch scan that slipped
# through as a single bill from costing a hundred page-images in one call.
MAX_BILL_PAGES = 8


def _encode_page(image):
    """One rendered PDF page as base64 JPEG, enhanced for reading."""
    from PIL import Image, ImageEnhance

    if image.mode != "RGB":
        image = image.convert("RGB")
    image = ImageEnhance.Contrast(image).enhance(1.2)
    image = ImageEnhance.Sharpness(image).enhance(1.1)

    width, height = image.size
    if width < 1000 or height < 1000:
        scale = max(1000 / width, 1000 / height)
        image = image.resize((int(width * scale), int(height * scale)),
                             Image.Resampling.LANCZOS)

    buf = BytesIO()
    image.save(buf, format="JPEG", quality=95)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("utf-8")


def _pdf_pages_from_bytes(pdf_bytes):
    """Every page of a PDF, not just the first.

    Rendering only page one meant a multi-page invoice silently lost
    everything after it — including the totals, which sit on the last
    page. The single-page helpers above are left as they were; this is
    what the analysis path uses now.
    """
    from pdf2image import convert_from_bytes

    if not pdf_bytes.startswith(b"%PDF"):
        raise ValueError("Invalid PDF file format")
    if len(pdf_bytes) < 100:
        raise ValueError("PDF file too small (possibly corrupted)")

    images = convert_from_bytes(
        pdf_bytes, first_page=1, last_page=MAX_BILL_PAGES, dpi=200, fmt="jpeg",
    )
    if not images:
        raise ValueError("No images generated from PDF")
    return [_encode_page(i) for i in images], "image/jpeg"


def prepare_bill_pages(file_path, file_name):
    """``(list_of_base64, mime)`` for a bill on disk. Images give one page."""
    if file_name.lower().endswith(".pdf"):
        with open(file_path, "rb") as f:
            return _pdf_pages_from_bytes(f.read())
    b64, mime = _prepare_raw_image(file_path, file_name.lower())
    return [b64], mime


def prepare_bill_pages_from_bytes(file_content, file_extension):
    """``(list_of_base64, mime)`` from raw bytes."""
    ext = (file_extension or "").lower().lstrip(".")
    if ext == "pdf":
        return _pdf_pages_from_bytes(file_content)
    b64, mime = prepare_bill_image_from_bytes(file_content, file_extension)
    return [b64], mime


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


_SHADOW_FIELDS = ("invoiceNumber", "dateIssued", "total", "igst", "cgst", "sgst")


def _shadow_agreement(live, shadow):
    """Per-field agreement between the live result and the shadow one.

    Field names only, never values: this goes to the application log and
    the values are client invoice data.
    """
    if not isinstance(live, dict) or not isinstance(shadow, dict):
        return {"comparable": False}
    same, differ = [], []
    for f in _SHADOW_FIELDS:
        a, b = live.get(f), shadow.get(f)
        if a in (None, "") and b in (None, ""):
            continue
        (same if str(a).strip() == str(b).strip() else differ).append(f)
    return {"comparable": True, "same": same, "differ": differ,
            "items_live": len(live.get("items") or []),
            "items_shadow": len(shadow.get("items") or [])}


def _with_checks(json_data):
    """Attach arithmetic and identity findings to the analysed data.

    Runs on every bill whichever model read it: a vision reading can be
    internally inconsistent in exactly the ways a rules reading can, and
    the same arithmetic catches both. Findings are advisory — they are
    shown to the operator, never used to reject a bill.
    """
    try:
        from apps.common.services.extraction.checks import run_checks

        # The OCR text rides on the payload rather than in a module
        # global: these run on RQ workers, and a global would leak one
        # bill's text into the next bill's checks.
        ctx = json_data.pop("_ocr_context", {}) if isinstance(json_data, dict) else {}
        findings = run_checks(
            json_data,
            document_text=ctx.get("text"),
            page_count=ctx.get("pages"),
        )
        if findings and isinstance(json_data, dict):
            json_data["_extraction_checks"] = findings
    except Exception:
        logger.exception("Extraction checks failed; continuing without them")
    return json_data


def _ocr_first_enabled(bill):
    """Global switch, or this bill's organisation on the rollout list.

    Read from the admin row per call, so flipping it needs neither a
    restart nor a deploy.
    """
    from apps.common.extraction_config import get_extraction_config

    return get_extraction_config().ocr_first_for(getattr(bill, "organization_id", None))


def _record_extraction(bill, telemetry):
    """Keep the route and its cost on the bill's own history.

    The saving is a claim about token counts; recorded per bill it stops
    being a claim. Values from the document never go in — only how it was
    read.

    The ``summary`` is the line a client reads on the Activity page, so
    it names no vendor and no model. Which model read a bill is our
    engineering concern, not something to put in front of the people
    whose books it posts to — it invites questions we do not want to
    field and ties us publicly to a supplier we may change. The real
    model and route stay in ``metadata`` for the admin-only accuracy
    page, which is where that detail belongs.
    """
    if bill is None:
        return
    try:
        from apps.common.models import BillActivity
        from apps.common.services.activity import record_activity

        record_activity(
            bill, BillActivity.Action.EXTRACTED,
            summary="Read by the AI service",
            metadata=telemetry,
        )
    except Exception:
        logger.exception("Could not record extraction telemetry for bill %s",
                         getattr(bill, "pk", "?"))


def _call_vision_with_optional_assist(images, mime, prompt, *, file_path=None,
                                      file_bytes=None, extension=None, bill=None):
    """The vision call, with OCR text alongside the pages when enabled.

    This is the default route plus an extra input, not a different
    route: the model still sees every page. If OCR fails, or the flag is
    off, the call is byte-for-byte the one that ships today.

    The image detail only drops when there is text to make up for it.
    Dropping it on its own would trade accuracy for fractions of a cent,
    which is the wrong direction on a document that becomes a ledger
    entry.
    """
    from apps.common.extraction_config import get_extraction_config

    config = get_extraction_config()
    if not config.ocr_assist:
        return call_vision_analysis(images, mime, prompt)

    ocr_text, detail = None, None
    try:
        from apps.common.services.extraction.ocr import ocr_document

        result = ocr_document(file_path=file_path, file_bytes=file_bytes,
                              extension=extension)
        if result.ok and result.text.strip():
            ocr_text = result.text
            detail = config.vision_detail
            _record_extraction(bill, {
                "route": "assist",
                "route_reason": f"OCR confidence {result.mean_confidence:.2f}",
                "model": config.vision_model,
                "detail": detail,
                "ocr_confidence": result.mean_confidence,
            })
    except Exception:
        # OCR is an optimisation here, never a dependency.
        logger.exception("OCR assist failed; sending the pages on their own")

    return call_vision_analysis(images, mime, prompt,
                                ocr_text=ocr_text, detail=detail)


def _extract(prompt, images, mime, *, file_path=None, file_bytes=None,
             extension=None, bill=None):
    """Vision on the image, unless the OCR-first route is switched on.

    Default is the behaviour this replaced, line for line. The flag is
    read per call rather than at import so a rollout can be flipped
    without a restart, and any failure inside the new route falls back
    here rather than failing the bill.
    """
    if not _ocr_first_enabled(bill):
        live = _call_vision_with_optional_assist(
            images, mime, prompt,
            file_path=file_path, file_bytes=file_bytes, extension=extension,
            bill=bill,
        )
        from apps.common.extraction_config import get_extraction_config

        if get_extraction_config().ocr_shadow:
            from apps.common.services.extraction.route import analyse

            # Run the new route for the log only and return the old result
            # regardless, so a wrong answer from it cannot reach a client.
            # Costs both calls; that is the price of finding out safely.
            try:
                shadow, telemetry = analyse(
                    prompt, images=images, mime=mime,
                    file_path=file_path, file_bytes=file_bytes, extension=extension,
                )
                telemetry.pop("ocr_text", None)
                telemetry = {**telemetry, "shadow": True,
                             "agreement": _shadow_agreement(live, shadow)}
                logger.info("extraction shadow: %s", telemetry)
                _record_extraction(bill, telemetry)
            except Exception:
                logger.exception("extraction shadow run failed (live result unaffected)")
        return live

    from apps.common.services.extraction.route import analyse

    try:
        data, telemetry = analyse(
            prompt, images=images, mime=mime,
            file_path=file_path, file_bytes=file_bytes, extension=extension,
        )
        # Strip the document text first. Everything below either logs or
        # stores this dict, and the text is client invoice content.
        ocr_text = telemetry.pop("ocr_text", None)
        if ocr_text and isinstance(data, dict):
            data["_ocr_context"] = {"text": ocr_text,
                                    "pages": telemetry.get("ocr_pages")}
        logger.info("extraction route: %s", telemetry)
        _record_extraction(bill, telemetry)
        return data
    except Exception:
        logger.exception("OCR-first route failed; using vision on the pages")
        return call_vision_analysis(images, mime, prompt)


def analyze_bill_bytes(file_content, file_extension, prompt, bill=None):
    """Full pipeline from raw bytes: bytes -> image -> OpenAI OCR -> validated JSON dict.

    Convenience wrapper for Zoho-style callers that receive ``(bytes, ext)``
    instead of ``(file_path, file_name)``.
    """
    images, mime = prepare_bill_pages_from_bytes(file_content, file_extension)
    json_data = _extract(prompt, images, mime, file_bytes=file_content,
                         extension=file_extension, bill=bill)
    return _with_checks(reconcile_item_line_amounts(ensure_gst_fields(json_data)))


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


def call_vision_analysis(images, mime_type, prompt, model=None,
                         max_tokens=2000, temperature=0.1,
                         ocr_text=None, detail=None):
    """Send every page of a bill to the vision model in one call.

    ``images`` is a list of base64 strings, one per page, in order. Sending
    them together rather than one at a time is the point: a total on page
    three belongs to the items on page one, and two separate calls cannot
    put them back together.

    ``ocr_text``, when given, rides alongside the pages rather than
    instead of them. The model then has the characters already read for
    it and the page to see where they sit, which are different jobs: OCR
    is good at glyphs and bad at knowing which column a number is in,
    and the model is the other way round.

    ``detail`` overrides the per-image detail level. With OCR text
    present, "low" is usually enough — the text carries the figures and
    the image only has to carry the layout — and a low-detail page costs
    a flat 85 tokens against roughly 1,200 for a high-detail scan.
    Measured on this client's bills: input drops from ~1,230 tokens to
    ~370, about 27% off the total call once output is counted. Left as
    "high" by default, because dropping detail without the text to make
    up for it would be trading accuracy for pennies.
    """
    client = _get_openai_client()
    from apps.common.extraction_config import get_extraction_config

    config = get_extraction_config()
    model = model or config.vision_model
    detail = detail or config.vision_detail

    content = [{"type": "text", "text": prompt}]
    if len(images) > 1:
        content.append({"type": "text", "text":
            f"This bill has {len(images)} pages, in order. Read them as one "
            "document: later pages continue the item list and usually carry "
            "the totals."})
    if ocr_text and ocr_text.strip():
        content.append({"type": "text", "text":
            "An OCR pass over the same pages produced the text below. Use it "
            "for the characters — it is usually right about digits and "
            "spellings — and use the image to decide what each value IS: "
            "which column a number sits in, which party a name belongs to, "
            "which row an amount belongs to. Where the two disagree, trust "
            "the image.\n\n--- OCR TEXT ---\n" + ocr_text.strip()})
    for b64 in images:
        content.append({"type": "image_url", "image_url": {
            "url": f"data:{mime_type};base64,{b64}", "detail": detail}})

    response = client.chat.completions.create(
        model=model, response_format={"type": "json_object"},
        messages=[{"role": "user", "content": content}],
        max_tokens=max_tokens, temperature=temperature,
    )
    return parse_openai_json_response(response.choices[0].message.content)


def call_text_analysis(document_text, prompt, model=None,
                       max_tokens=2000, temperature=0.1):
    """Extract from OCR text instead of a page image.

    Same prompt, same JSON contract, same parser as the vision call — the
    only difference is that the document arrives as text somebody else
    already read. That makes it a much easier job, which is why a small
    model is enough for it.
    """
    client = _get_openai_client()
    from apps.common.extraction_config import get_extraction_config

    model = model or get_extraction_config().text_model

    response = client.chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        messages=[{
            "role": "user",
            "content": (
                f"{prompt}\n\n"
                "The document below was read by OCR. Work only from it; do not "
                "invent values it does not contain, and leave a field null when "
                "the document does not state it.\n\n"
                "--- DOCUMENT ---\n"
                f"{document_text}"
            ),
        }],
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return parse_openai_json_response(response.choices[0].message.content)


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
    
    🏢 WHO IS "from" (THE VENDOR / SUPPLIER) — READ CAREFULLY:
    - "from" is the SUPPLIER / SELLER / SERVICE PROVIDER who issued the
      invoice and charges the money — usually at the top with its logo,
      letterhead, address and "GSTIN of Supplier".
    - NOT "from": the Bill To / Billed To / Invoice To / Customer / Buyer /
      Guest / Ship To / Consignee party — that party is "to".
    - NOT "from": a software, portal or platform named on the invoice —
      e.g. "Generated by", "Powered by", "Invoice generated using",
      "e-Invoice / IRN via", GSP / ASP / accounting-software or payment
      gateway names, bank names. Ignore them for both "from" and "to".
    - If the supplier's name appears ONLY as a logo or stylised graphic,
      read the name from the logo (e.g. a "make my trip" logo -> "MakeMyTrip").
    - "from.name" and "from.gst_number" MUST belong to the SAME business.
      Check: for a company or firm, the 7th character of the GSTIN (the
      5th character of its PAN) is the FIRST LETTER of the business name
      (e.g. 06AADC**M**5146R1ZZ -> a name starting with "M"). If the name
      you picked does not start with that letter, you picked the wrong
      party's name — find the name printed with that GSTIN. Apply the same
      check to "to".

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
       not, RE-READ the digits of every offending field. For LARGE
       printed amounts (comma-grouped, 6+ digits) a missed leading
       digit (₹5,82,338 read as ₹82,338) is the #1 OCR failure mode —
       there, prefer the higher digit count when the comma grouping or
       the amount in words supports it. Do NOT add digits to small or
       handwritten amounts to make the arithmetic fit — an EXTRA
       digit is just as wrong (see rule 8).
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
    7. PAISE / DECIMALS — every MONEY amount (item amount, igst,
       cgst, sgst, tds, round_off, discount, cess, freight, total)
       MUST keep BOTH digits after the decimal point exactly as printed.
       "62,816.13" -> 62816.13 (NOT 62816.1, NOT 62816).
       "11,306.90" -> 11306.90. "74,123.00" -> 74123.00.
       Read the last two digits after the decimal point one by one —
       dropping the final paise digit is a known OCR failure. If the
       same amount is printed in more than one place (item row,
       taxable value, tax summary table), use those copies to confirm
       the paise digits. Quantity and unit price/rate keep EVERY
       decimal digit that is printed (e.g. 1.125, 12.3456) — never
       round or truncate them.
    8. "/-" AFTER A NUMBER IS NOT A DIGIT. Indian bills write whole
       rupee amounts as "425/-", "1,250/-", "Rs. 850/-" — the "/-"
       means "rupees only, no paise". "425/-" -> 425 (NOT 4250, NOT
       4251). On handwritten bills the "/-" often looks like "0", "1"
       or "7" — never read it as a digit.
    9. LINE ARITHMETIC: for every line, rate x quantity must equal the
       line amount printed in the Amount column. If it does not, re-read
       the rate, quantity and amount. If the Qty column is blank or
       unreadable but the amount is a whole multiple of the rate
       (425 and 850), the quantity is that multiple (2). Always return
       the printed line amount in the item's "amount" field.

    Return a single JSON object matching the schema below. Do NOT
    include commentary, markdown fences, or narration.
"""


def reconcile_item_line_amounts(json_data):
    """Fix a line whose rate / quantity don't add up to its printed amount.

    Every consumer (Tally + Zoho) builds a line as ``price × quantity``, so
    a misread rate ("425/-" read as 4250) or a missed quantity silently
    inflates the bill. The prompt asks OCR for each row's printed
    ``amount``; this trusts that amount — but ONLY when doing so makes the
    bill add up to its printed total (Σ lines + taxes + adjustments),
    i.e. when the arithmetic proves the rate/qty was the misread part.

    Per mismatched line:
      - qty missing / 1 and amount is a whole multiple (≥2) of the rate
        -> the quantity was missed: quantity = amount / rate.
      - otherwise -> keep the quantity, rate = amount / quantity.

    Mutates ``json_data`` in place, records each change under
    ``_ocr_line_fixes`` (shown on the verify screen) and returns it.
    Never raises — any problem leaves the data untouched.
    """
    try:
        if not isinstance(json_data, dict):
            return json_data
        items = json_data.get("items")
        if not isinstance(items, list) or not items:
            return json_data

        def _f(v):
            try:
                return float(str(v).replace(",", "").strip() or 0)
            except (TypeError, ValueError):
                return 0.0

        printed_total = _f(json_data.get("total"))
        if printed_total <= 0:
            return json_data

        extras = sum(_f(json_data.get(k)) for k in (
            "igst", "cgst", "sgst", "cess", "discount", "freight", "round_off",
        ))

        rows = []
        mismatched = False
        for row in items:
            if not isinstance(row, dict):
                rows.append(None)
                continue
            price, qty, amount = _f(row.get("price")), _f(row.get("quantity")), _f(row.get("amount"))
            pq = price * qty
            bad = amount > 0 and abs(pq - amount) > max(1.0, amount * 0.005)
            mismatched = mismatched or bad
            rows.append((price, qty, amount, pq, bad))
        if not mismatched:
            return json_data

        sum_pq = sum(r[3] for r in rows if r)
        sum_amt = sum((r[2] if r[2] > 0 else r[3]) for r in rows if r)
        drift_pq = abs(sum_pq + extras - printed_total)
        drift_amt = abs(sum_amt + extras - printed_total)
        tolerance = max(5.0, printed_total * 0.01)
        # Only act when the printed amounts reconcile the bill and the
        # rate × qty figures don't.
        if not (drift_amt <= tolerance and drift_amt < drift_pq):
            return json_data

        fixes = []
        for idx, (row, r) in enumerate(zip(items, rows)):
            if not r or not r[4]:
                continue
            price, qty, amount, _pq, _bad = r
            new_price, new_qty = price, qty
            multiple = amount / price if price > 0 else 0
            if qty <= 1 and multiple >= 2 and abs(multiple - round(multiple)) < 0.01:
                new_qty = float(round(multiple))
            else:
                new_qty = qty if qty > 0 else 1.0
                new_price = round(amount / new_qty, 6)
            row["price"] = new_price
            row["quantity"] = int(new_qty) if float(new_qty).is_integer() else new_qty

            # Distinguish a genuine correction from simply filling a blank.
            #
            # Summary-style bills (hospital bills, service invoices) print
            # only an amount per line — no rate, no quantity column. OCR
            # then reports 0 x 0 for every row and this routine sets
            # rate = amount, qty = 1. That is the right answer, but it is
            # not a "correction of a misread": rate x 1 = amount is a
            # tautology with nothing for the operator to check. Reporting
            # all fourteen of those as individual warnings buried the one
            # line that mattered ("14 products are missing item names").
            #
            # A real correction is when OCR DID read a rate or quantity and
            # it disagreed with the printed amount — those still deserve a
            # per-line callout.
            trivial_fill = price <= 0 and qty <= 1 and row["quantity"] == 1
            fixes.append({
                "index": idx,
                "kind": "filled" if trivial_fill else "corrected",
                "description": str(row.get("description") or "")[:120],
                "from": {"price": price, "quantity": qty},
                "to": {"price": new_price, "quantity": row["quantity"]},
                "amount": amount,
            })
        if fixes:
            json_data["_ocr_line_fixes"] = fixes
            corrected = [f for f in fixes if f["kind"] == "corrected"]
            # A page of blank-fills is routine for summary-style bills and
            # does not deserve a warning; a genuine misread does.
            if corrected:
                logger.warning(
                    "OCR line amounts reconciled to printed amounts "
                    "(%d corrected, %d filled): %s",
                    len(corrected), len(fixes) - len(corrected), corrected,
                )
            else:
                logger.info(
                    "Filled rate/qty from printed amount on %d line(s) — "
                    "no rate or quantity column on this bill.",
                    len(fixes),
                )
        return json_data
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("reconcile_item_line_amounts skipped: %s", exc)
        return json_data


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
        # On an MRP-priced bill the line amounts already hold their
        # GST, so adding the tax again double-counts it and every such
        # bill reads as a mismatch of exactly its own tax.
        if relevant_data.get('lines_include_gst'):
            tax_sum = 0.0
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
        # A missed leading digit typically = printed_value × 10. The
        # opposite also happens — an EXTRA digit ("425/-" read as 4250),
        # where dividing that field by 10 aligns the totals.
        suspects = []
        extra_digit = []
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
            elif abs(computed - (printed - val + val / 10)) < 1.0:
                extra_digit.append(label)
        for idx, r in enumerate(items):
            amt = _line_amount(r)
            if amt <= 0:
                continue
            if abs(computed - amt + amt * 10 - printed) < 1.0:
                suspects.append(f"item[{idx}]")
            elif abs(computed - amt + amt / 10 - printed) < 1.0:
                extra_digit.append(f"item[{idx}]")

        msg = (
            f"OCR sanity check: computed ₹{computed:,.2f} vs printed ₹{printed:,.2f} "
            f"(drift ₹{drift:,.2f} = {drift_pct:.1f}%)."
        )
        if suspects:
            msg += f" Likely missed leading digit on: {', '.join(suspects)}."
        if extra_digit:
            msg += f" Likely an extra digit on: {', '.join(extra_digit)}."

        return {
            "ok": False,
            "drift": drift,
            "drift_pct": drift_pct,
            "message": msg,
            "suspects": suspects,
            "extra_digit_suspects": extra_digit,
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
                "hsn": "HSN or SAC code for this line, or empty string",
                "unit": "Unit as printed (BAGS, NOS, KG...) or empty string",
                "gst_rate": 0,
                "quantity": 0,
                "price": 0,
                "amount": 0
            }
        ],
        "hsn_summary": [
            {
                "hsn": "HSN/SAC code",
                "taxable_value": 0,
                "cgst_rate": 0, "cgst_amount": 0,
                "sgst_rate": 0, "sgst_amount": 0,
                "igst_rate": 0, "igst_amount": 0
            }
        ],
        "is_reverse_charge": false,
        "discount_before_tax": false,
        "freight_taxable": false,
        "lines_include_gst": false,
        "place_of_supply": "State name and/or code as printed, or empty string",
        "irn": "e-invoice IRN if printed, else empty string",
        "ack_no": "e-invoice acknowledgement number if printed, else empty string",
        "total": 0,
        "igst": 0,
        "cgst": 0,
        "sgst": 0,
        "round_off": 0,
        "discount": 0,
        "cess": 0,
        "freight": 0
    }

    ITEM FIELDS: "price" = rate per unit, "quantity" = qty, "amount" =
    the line total printed in that row's Amount column (0 if the bill
    prints no line amount). "price" x "quantity" should equal "amount".
    "hsn" = the HSN/SAC code printed in that row. "unit" = the unit as
    printed. "gst_rate" = that line's GST rate as a PERCENTAGE NUMBER
    (5 for 5%, 18 for 18%, 0 for exempt). If you cannot tell, OMIT the
    key rather than guessing 0 — a wrong 0 removes tax that is due.

    📊 THE HSN/SAC SUMMARY TABLE — READ IT, IT IS AUTHORITATIVE
    Almost every GST tax invoice carries a summary table near the
    bottom, one row per HSN/SAC:

        HSN/SAC   Taxable Value  CGST Rate  Amount  SGST Rate  Amount
        19021900       9,047.60      2.50%  226.19      2.50%  226.19
        11042900      14,400.00         0%                  0%

    Put every row in "hsn_summary". This table is the most reliable
    thing on the invoice: legally mandated, it states the rate per HSN
    explicitly, and its "Taxable Value" column is the figure tax was
    actually charged on. Use it to fill each item's "gst_rate" by
    matching on HSN. Where the table and your reading of a line
    disagree, TRUST THE TABLE.

    CGST and SGST rates are each HALF the total rate — a row showing
    2.50% + 2.50% is a 5% item, so "gst_rate" is 5, not 2.5. An IGST row
    shows the full rate, so IGST 18% means "gst_rate" 18.

    💰 "discount_before_tax" — this changes how much tax is due:
      - true  — the discount is deducted from the sub-total and GST is
                charged on the reduced figure. Tell-tales: the discount
                row sits ABOVE the tax lines, or the HSN summary's total
                "Taxable Value" is LESS than the sum of line amounts.
      - false — deducted after tax, near the grand total.
    No discount on the bill → leave it false.

    🔄 "is_reverse_charge": true when the invoice says the tax is
    payable by the RECIPIENT, not the supplier. The wording is almost
    always one of: "Tax payable under reverse charge", "GST payable by
    recipient under RCM", "Reverse Charge: Yes", or an "RCM" column
    marked Y. A bill under reverse charge usually shows the tax rate but
    NO tax amount, because the supplier does not charge it.

    🚚 "freight_taxable":
      - true  — freight/packing/forwarding appears ABOVE the tax lines,
                or is inside the HSN summary's taxable value.
      - false — added after tax, near the grand total.

    🏷️ "lines_include_gst":
      - true  — the printed line amounts ALREADY contain their GST.
                Tell-tales: an MRP column, a pharmacy / retail / FMCG
                bill, "Rate (incl. GST)", "Price incl. tax", or the
                line amounts summing to the grand total with the tax
                shown only as a memo ("CGST+SGST = 18.92") rather than
                added to it.
      - false — GST is added on top of the line amounts, i.e. the
                lines plus the tax equal the total. This is the
                default; only set true when the arithmetic or a
                printed label says so.

    🧾 E-INVOICE: if "IRN" and "Ack No." are printed (usually top-left
    beside a QR code), extract them exactly. The IRN is a 64-character
    hex string and identifies this invoice uniquely on the government
    portal.

    📍 "place_of_supply": extract it as printed. It decides inter-state
    (IGST) vs intra-state (CGST+SGST) and is NOT always the buyer's
    registered state.

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


def get_sales_bill_prompt():
    """Return the OpenAI prompt for sales invoice analysis (items-based).

    Same JSON shape as the vendor prompt on purpose — a sales invoice
    and a purchase bill are the same document read from opposite ends,
    and one shape means one parser downstream. ``from`` is still the
    issuer and ``to`` still the recipient; on this side the issuer is
    the organisation itself and the party we post against is ``to``.
    Saying so explicitly matters: the model will otherwise put the
    better-printed name, usually the letterhead, in whichever field it
    thinks is the "main" one.
    """
    return (
        "Analyze this Indian SALES invoice image very carefully and extract "
        "ALL visible information in JSON format. This invoice was ISSUED BY "
        "the seller on the letterhead and SENT TO a customer.\n"
        + _GST_EXTRACTION_PREAMBLE
        + """
    🎯 MANDATORY JSON STRUCTURE:
    {
        "invoiceNumber": "Invoice number as shown on document",
        "dateIssued": "Invoice date in YYYY-MM-DD format",
        "dueDate": "Due date in YYYY-MM-DD format if mentioned, empty string if not",
        "from": {
            "name": "SELLER name — whoever ISSUED this invoice, normally the letterhead at the top",
            "address": "Complete seller address",
            "gst_number": "Seller's GST number or empty string if not found - FIELD IS MANDATORY"
        },
        "to": {
            "name": "BUYER name — the customer this invoice is billed to, normally under 'Bill To', 'Buyer', 'Customer', 'Party', 'Consignee' or 'M/s'",
            "address": "Complete buyer address",
            "gst_number": "Buyer's GST number or empty string if not found - FIELD IS MANDATORY"
        },
        "items": [
            {
                "description": "Item/Service description",
                "hsn": "HSN or SAC code for this line, or empty string",
                "unit": "Unit as printed (BAGS, NOS, KG...) or empty string",
                "gst_rate": 0,
                "quantity": 0,
                "price": 0,
                "amount": 0
            }
        ],
        "hsn_summary": [
            {
                "hsn": "HSN/SAC code",
                "taxable_value": 0,
                "cgst_rate": 0, "cgst_amount": 0,
                "sgst_rate": 0, "sgst_amount": 0,
                "igst_rate": 0, "igst_amount": 0
            }
        ],
        "is_reverse_charge": false,
        "discount_before_tax": false,
        "freight_taxable": false,
        "lines_include_gst": false,
        "place_of_supply": "State name and/or code as printed, or empty string",
        "irn": "e-invoice IRN if printed, else empty string",
        "ack_no": "e-invoice acknowledgement number if printed, else empty string",
        "total": 0,
        "igst": 0,
        "cgst": 0,
        "sgst": 0,
        "round_off": 0,
        "discount": 0,
        "cess": 0,
        "freight": 0
    }

    WHO IS WHO — read this before filling "from" and "to":
      - "from" is the SELLER. The letterhead, logo and the GSTIN printed
        at the very top belong to the seller.
      - "to" is the BUYER, printed under "Bill To", "Billed To",
        "Buyer", "Customer", "Party", "M/s" or "Consignee".
      - If the invoice shows both "Bill To" and "Ship To" with different
        names, "to" is the "Bill To" one.
      - Never copy the same name into both. If only one party is legible,
        fill that one and leave the other's fields as empty strings.

    ITEM FIELDS: "price" = rate per unit, "quantity" = qty, "amount" =
    the line total printed in that row's Amount column (0 if the invoice
    prints no line amount). "price" x "quantity" should equal "amount".
    "hsn" = the HSN/SAC code printed in that row. "unit" = the unit as
    printed. "gst_rate" = that line's GST rate as a PERCENTAGE NUMBER
    (5 for 5%, 18 for 18%, 0 for exempt). If you cannot tell, OMIT the
    key rather than guessing 0 — a wrong 0 removes tax that is due.

    📊 THE HSN/SAC SUMMARY TABLE — READ IT, IT IS AUTHORITATIVE
    Almost every GST tax invoice carries a summary table near the
    bottom, one row per HSN/SAC:

        HSN/SAC   Taxable Value  CGST Rate  Amount  SGST Rate  Amount
        19021900       9,047.60      2.50%  226.19      2.50%  226.19
        11042900      14,400.00         0%                  0%

    Put every row in "hsn_summary". This table is the most reliable
    thing on the invoice: legally mandated, it states the rate per HSN
    explicitly, and its "Taxable Value" column is the figure tax was
    actually charged on. Use it to fill each item's "gst_rate" by
    matching on HSN. Where the table and your reading of a line
    disagree, TRUST THE TABLE.

    CGST and SGST rates are each HALF the total rate — a row showing
    2.50% + 2.50% is a 5% item, so "gst_rate" is 5, not 2.5. An IGST row
    shows the full rate, so IGST 18% means "gst_rate" 18.

    💰 "discount_before_tax" — this changes how much tax is due:
      - true  — the discount is deducted from the sub-total and GST is
                charged on the reduced figure. Tell-tales: the discount
                row sits ABOVE the tax lines, or the HSN summary's total
                "Taxable Value" is LESS than the sum of line amounts.
      - false — deducted after tax, near the grand total.
    No discount on the bill → leave it false.

    🔄 "is_reverse_charge": true when the invoice says the tax is
    payable by the RECIPIENT, not the supplier. The wording is almost
    always one of: "Tax payable under reverse charge", "GST payable by
    recipient under RCM", "Reverse Charge: Yes", or an "RCM" column
    marked Y. A bill under reverse charge usually shows the tax rate but
    NO tax amount, because the supplier does not charge it.

    🚚 "freight_taxable":
      - true  — freight/packing/forwarding appears ABOVE the tax lines,
                or is inside the HSN summary's taxable value.
      - false — added after tax, near the grand total.

    🏷️ "lines_include_gst":
      - true  — the printed line amounts ALREADY contain their GST.
                Tell-tales: an MRP column, a pharmacy / retail / FMCG
                bill, "Rate (incl. GST)", "Price incl. tax", or the
                line amounts summing to the grand total with the tax
                shown only as a memo ("CGST+SGST = 18.92") rather than
                added to it.
      - false — GST is added on top of the line amounts, i.e. the
                lines plus the tax equal the total. This is the
                default; only set true when the arithmetic or a
                printed label says so.

    🧾 E-INVOICE: if "IRN" and "Ack No." are printed (usually top-left
    beside a QR code), extract them exactly. The IRN is a 64-character
    hex string and identifies this invoice uniquely on the government
    portal.

    📍 "place_of_supply": extract it as printed. It decides inter-state
    (IGST) vs intra-state (CGST+SGST) and is NOT always the buyer's
    registered state.

    round_off / discount / cess / freight EXTRACTION RULES:
      - If the invoice explicitly prints a line like
        "ROUND OFF VALUE (₹)", "Rounded Off", "R/O" — put that
        signed amount in ``round_off`` (may be negative).
      - If the invoice prints "Discount", "Trade Discount",
        "Less: Discount" — put the amount in ``discount`` as a SIGNED
        value: a discount that reduces the invoice is NEGATIVE
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


def get_receipt_bill_prompt():
    """Return the OpenAI prompt for receipt voucher analysis.

    Same JSON shape as the expense prompt the payment voucher reuses, so
    the downstream parser is unchanged. What it adds is who is who: a
    receipt records money arriving from a customer, and without being
    told so the model reports the organisation's own letterhead as the
    party.
    """
    return (
        "Analyze this Indian RECEIPT / payment-advice document very carefully "
        "and extract ALL visible information in JSON format. It records money "
        "RECEIVED by the issuer FROM a customer.\n"
        + _GST_EXTRACTION_PREAMBLE
        + """
    🎯 MANDATORY JSON STRUCTURE:
    {
        "invoiceNumber": "Receipt/Voucher number as shown on document",
        "dateIssued": "Receipt date in YYYY-MM-DD format",
        "dueDate": "Due date in YYYY-MM-DD format if mentioned, empty string if not",
        "from": {
            "name": "ISSUER name — whoever issued this receipt, normally the letterhead",
            "address": "Complete issuer address",
            "gst_number": "Issuer's GST number or empty string if not found - FIELD IS MANDATORY"
        },
        "to": {
            "name": "PAYER name — the customer the money came from, normally under 'Received From', 'Received With Thanks From' or 'M/s'",
            "address": "Complete payer address",
            "gst_number": "Payer's GST number or empty string if not found - FIELD IS MANDATORY"
        },
        "expenses": [
            {
                "description": "What the amount is against — invoice reference, period, or purpose",
                "category": "Category if stated",
                "amount": 0
            }
        ],
        "total": 0,
        "igst": 0,
        "cgst": 0,
        "sgst": 0,
        "tds": 0
    }

    WHO IS WHO:
      - "from" is the ISSUER of the receipt — the party that RECEIVED
        the money. Letterhead and top-of-page GSTIN belong to them.
      - "to" is the PAYER — the customer, usually printed after
        "Received From", "Received With Thanks From", "Party" or "M/s".
      - Never copy the same name into both. If only one is legible, fill
        that one and leave the other's fields as empty strings.

    AMOUNT RULES:
      - "total" is the amount actually received. If the document prints
        an amount in words, use it to confirm the figure.
      - If the receipt settles specific invoices, put one entry per
        invoice in "expenses" with its reference in "description".
      - "tds" only when TDS is explicitly printed as deducted.
      - If a field is absent, return 0 or an empty string. Extract only
        what is printed; do not derive.
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

def analyze_bill_file(file_path, file_name, prompt, bill=None):
    """Full pipeline: read file → image → OpenAI OCR → validated JSON dict.

    Args:
        file_path: Absolute path to the bill file.
        file_name: Original filename (used for extension detection).
        prompt: The prompt string to send to OCR engine.

    Returns:
        ``dict`` — the extracted JSON data (with GST fields ensured).
    Raises on failure.
    """
    images, mime = prepare_bill_pages(file_path, file_name)
    json_data = _extract(prompt, images, mime, file_path=file_path, bill=bill)
    return _with_checks(reconcile_item_line_amounts(ensure_gst_fields(json_data)))

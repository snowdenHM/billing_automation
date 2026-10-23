"""Choose how a bill gets read, and by which model.

Measured on 96 real client bills: indra-ocr reads them well (median
confidence 0.95, tables on 73%), while the rules that turned that text
into fields found an invoice number on 11% and a date on 9%. So the OCR
is the part that works, and the model is the part that should do the
field extraction.

That makes the common path cheap. The OCR text of a bill is roughly 900
tokens; the scan it came from is several megabytes, and an image costs
far more to send than the text it contains. A small model reading clean
text is doing a much easier job than a large one squinting at a scan, so
the common path is text to the cheap model.

The expensive path stays for the bills where OCR genuinely has nothing
to give — handwritten shop bills, bad scans. 29% of the 96 fell below
0.90 confidence; those still go to the vision model as an image, exactly
as every bill does today.

Everything here is off unless EXTRACTION_OCR_FIRST is set. With it unset
the caller never reaches this module.
"""

import logging

from django.conf import settings

from .ocr import ocr_document

logger = logging.getLogger(__name__)


def _setting(name, default):
    return getattr(settings, name, default)


def choose_route(ocr_result):
    """``("text", why)`` or ``("vision", why)``.

    Separated from the call so the decision can be tested, and so shadow
    mode can record what it *would* have chosen without spending
    anything.
    """
    min_conf = _setting("EXTRACTION_OCR_MIN_CONFIDENCE", 0.90)
    min_chars = _setting("EXTRACTION_OCR_MIN_CHARS", 200)

    if not ocr_result.ok:
        return "vision", ocr_result.error or "OCR produced no text"
    if ocr_result.mean_confidence < min_conf:
        return "vision", (f"OCR confidence {ocr_result.mean_confidence:.2f} "
                          f"below {min_conf:.2f}")
    if len(ocr_result.text.strip()) < min_chars:
        # A nearly empty page at high confidence is the handwriting case:
        # the few printed words it did read are read well.
        return "vision", f"only {len(ocr_result.text.strip())} characters of text"
    return "text", f"OCR confidence {ocr_result.mean_confidence:.2f}"


def analyse(prompt, *, image_b64, mime, file_path=None, file_bytes=None,
            extension=None):
    """Extract a bill, cheaply where that is safe. Returns ``(json, telemetry)``.

    Falls back to the vision model on any OCR or text-model failure, so
    the worst case is the behaviour this replaces plus the cost of an
    attempt.
    """
    from apps.common.services.bill_analysis import (
        call_ocr_analysis, call_text_analysis,
    )

    text_model = _setting("EXTRACTION_TEXT_MODEL", "gpt-4o-mini")
    vision_model = _setting("EXTRACTION_VISION_MODEL", "gpt-4o")

    result = ocr_document(file_path=file_path, file_bytes=file_bytes,
                          extension=extension)
    route, why = choose_route(result)
    telemetry = {"route": route, "route_reason": why, **result.summary()}
    # Carried for the downstream checks, popped by the caller before the
    # telemetry is logged -- client document text must not reach the log.
    if result.ok:
        telemetry["ocr_text"] = result.text

    if route == "text":
        try:
            data = call_text_analysis(result.text, prompt, model=text_model)
            telemetry["model"] = text_model
            return data, telemetry
        except Exception as exc:
            # The cheap path failing is not the bill failing.
            logger.warning("Text extraction failed (%s); using %s on the image",
                           exc, vision_model)
            telemetry["text_model_error"] = f"{type(exc).__name__}: {exc}"[:200]
            telemetry["route"] = "vision"
            telemetry["route_reason"] = "text model failed"

    data = call_ocr_analysis(image_b64, mime, prompt, model=vision_model)
    telemetry["model"] = vision_model
    return data, telemetry

from __future__ import annotations

import base64
import io
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from pdf2image import convert_from_bytes
from PIL import Image


@dataclass
class LineItem:
    description: str
    quantity: float
    price: float


@dataclass
class ParsedInvoice:
    invoice_number: Optional[str]
    date_issued: Optional[str]
    total: Optional[float]
    igst: Optional[float]
    cgst: Optional[float]
    sgst: Optional[float]
    items: List[LineItem]


def _pdf_to_images(pdf_bytes: bytes) -> List[Image.Image]:
    """Convert a PDF to a list of PIL images (one per page)."""
    return convert_from_bytes(pdf_bytes)


def _image_bytes(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="JPEG")
    return buf.getvalue()


def quick_heuristic_parse(image_bytes: bytes) -> ParsedInvoice:
    """
    Placeholder/dummy parser that returns very basic stub data.
    Swap this with your LLM/vision call (OpenAI, etc.).
    """
    # TODO: integrate your real OCR/LLM here.
    # keep return shape stable
    return ParsedInvoice(
        invoice_number="INV-0001",
        date_issued=None,
        total=0.0,
        igst=0.0,
        cgst=0.0,
        sgst=0.0,
        items=[LineItem(description="Item", quantity=1, price=0.0)],
    )


def parse_single_invoice(file_bytes: bytes, is_pdf: bool) -> Tuple[ParsedInvoice, int]:
    """
    Single Invoice/File:
      - PDF may have multiple pages but represents one invoice. We can merge pages.
      - Strategy: take first page for key fields; items can be extended later.
    Returns (parsed_invoice, pages_count)
    """
    if is_pdf:
        pages = _pdf_to_images(file_bytes)
        first = pages[0]
        parsed = quick_heuristic_parse(_image_bytes(first))
        return parsed, len(pages)
    else:
        # assume image already
        parsed = quick_heuristic_parse(file_bytes)
        return parsed, 1


def split_multi_invoice(file_bytes: bytes) -> List[ParsedInvoice]:
    """
    Multiple Invoice/File:
      - One PDF where each page is a separate invoice.
    """
    pages = _pdf_to_images(file_bytes)
    result: List[ParsedInvoice] = []
    for p in pages:
        parsed = quick_heuristic_parse(_image_bytes(p))
        result.append(parsed)
    return result

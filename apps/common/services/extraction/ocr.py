"""Read a bill with indra-ocr and hand back its text.

This is the swappable seam. The parser and the router both consume the
shape returned here, and nothing else in the codebase knows which OCR
engine produced it — replacing indra-ocr means rewriting this file and
nothing more.

``indra_ocr`` is imported inside the call, never at module load, so a
deployment that has not installed it imports this module happily and
only fails if something actually asks for an OCR run.
"""

import logging
import os
import tempfile
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class OcrResult:
    text: str = ""
    mean_confidence: float = 0.0
    page_count: int = 0
    ms: int = 0
    pages: list = field(default_factory=list)
    error: str = ""

    @property
    def ok(self):
        return bool(self.text.strip()) and not self.error

    def summary(self):
        """What the router logs. Never the text itself — these are client
        documents and this goes to the application log."""
        return {
            "ocr_ok": self.ok,
            "ocr_chars": len(self.text),
            "ocr_confidence": round(self.mean_confidence, 3),
            "ocr_pages": self.page_count,
            "ocr_ms": self.ms,
            "ocr_error": self.error[:200] if self.error else "",
        }


def ocr_document(file_path=None, file_bytes=None, extension=None):
    """OCR a bill from a path or from bytes. Never raises.

    A failure comes back as an ``OcrResult`` with ``ok`` False and a
    reason, because the caller's answer to "OCR did not work" is to send
    the image to a vision model, not to fail the bill.
    """
    started = time.time()
    tmp = None
    try:
        if file_path is None:
            if not file_bytes:
                return OcrResult(error="neither a path nor bytes were given")
            suffix = extension if (extension or "").startswith(".") else f".{extension or 'pdf'}"
            fd, tmp = tempfile.mkstemp(suffix=suffix)
            with os.fdopen(fd, "wb") as f:
                f.write(file_bytes)
            file_path = tmp

        import indra_ocr

        conv = indra_ocr.load(threads=indra_ocr.usable_cores())
        pages, confs = [], []
        for index, page in conv.convert(file_path):
            text = page.markdown or "\n".join(l.text for l in page.lines)
            pages.append({"index": index, "text": text})
            confs.extend(l.conf for l in page.lines if l.conf is not None)

        return OcrResult(
            text="\n\n".join(p["text"] for p in pages),
            mean_confidence=(sum(confs) / len(confs)) if confs else 0.0,
            page_count=len(pages),
            ms=int((time.time() - started) * 1000),
            pages=pages,
        )
    except ImportError as exc:
        # Expected wherever the OCR package is not installed. Not an error
        # worth a traceback — the caller falls back to the vision model.
        logger.info("indra-ocr not available (%s); falling back to vision", exc)
        return OcrResult(error=f"indra-ocr not installed: {exc}",
                         ms=int((time.time() - started) * 1000))
    except Exception as exc:
        logger.exception("OCR failed")
        return OcrResult(error=f"{type(exc).__name__}: {exc}",
                         ms=int((time.time() - started) * 1000))
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass

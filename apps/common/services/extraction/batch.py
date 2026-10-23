"""Split an upload that turned out to be several bills in one file.

People scan a stack of shop bills into one PDF and upload it as a single
bill. Read as one document it becomes a voucher carrying thirty vendors'
items under one of their names. Read page by page it becomes thirty
bills, which is right — except when the file really is one invoice whose
items run past the first page, and splitting it strands the totals.

``extraction.pages`` makes that call from the OCR text. This applies it:
if the grouping says more than one bill, the original becomes one bill
per group, each keeping its own pages together, and each is queued for
analysis on its own.

Runs in the background job, not the upload request — it needs an OCR
pass, which has no business on a request thread. Off unless
EXTRACTION_SPLIT_BATCHES is set, and a no-op wherever indra-ocr is not
installed, so the default is the behaviour this sits beside.
"""

import logging

from django.conf import settings

logger = logging.getLogger(__name__)


def _pdf_page_count(bill):
    try:
        from io import BytesIO

        from PyPDF2 import PdfReader

        bill.file.seek(0)
        data = bill.file.read()
        return len(PdfReader(BytesIO(data)).pages), data
    except Exception:
        return 0, None
    finally:
        try:
            bill.file.close()
        except Exception:
            pass


def maybe_split(bill, *, enqueue_analysis_fn):
    """Split *bill* when its file holds several bills. Returns True if it did.

    True means the caller must stop: this bill has been replaced by the
    ones that came out of it, and each has its own job queued.
    """
    if not getattr(settings, "EXTRACTION_SPLIT_BATCHES", False):
        return False

    name = (getattr(bill.file, "name", "") or "").lower()
    if not name.endswith(".pdf"):
        return False

    try:
        page_count, pdf_bytes = _pdf_page_count(bill)
        if page_count < 2 or not pdf_bytes:
            return False

        from apps.common.services.extraction.ocr import ocr_document
        from apps.common.services.extraction.pages import describe, group_pages

        result = ocr_document(file_bytes=pdf_bytes, extension=".pdf")
        if not result.ok or not result.pages:
            # No text to decide on. Leaving it whole is the safer of the
            # two guesses: the analysis path reads every page anyway.
            logger.info("Batch split skipped for bill %s: %s",
                        bill.pk, result.error or "no OCR text")
            return False

        groups = group_pages([p["text"] for p in result.pages])
        logger.info("Batch check on bill %s: %s", bill.pk, describe(groups, page_count))
        if len(groups) < 2:
            return False

        from apps.common.services.pdf_processing import split_pdf_by_groups

        children = split_pdf_by_groups(
            pdf_bytes, groups, bill.organization,
            getattr(bill, "file_type", None) or getattr(bill, "fileType", None),
            bill.uploaded_by, type(bill),
        )
        if not children:
            return False

        _record(bill, children, groups, page_count)

        for child in children:
            try:
                enqueue_analysis_fn(str(child.pk))
            except Exception:
                logger.exception("Could not queue analysis for split bill %s", child.pk)

        # The original has been replaced by its parts; keeping it would
        # show the operator a duplicate of everything.
        bill.delete()
        return True
    except Exception:
        logger.exception("Batch split failed for bill %s; analysing it whole", bill.pk)
        return False


def _record(bill, children, groups, page_count):
    """Leave the split on each new bill's history, so a surprised operator
    can see where it came from."""
    try:
        from apps.common.models import BillActivity
        from apps.common.services.activity import record_activity
        from apps.common.services.extraction.pages import describe

        for child, group in zip(children, groups):
            record_activity(
                child, BillActivity.Action.UPLOADED,
                organization=bill.organization,
                summary=describe(groups, page_count),
                metadata={
                    "split_from": str(bill.pk),
                    "source_pages": [i + 1 for i in group.get("pages", [])],
                    "reason": group.get("reason", ""),
                },
            )
    except Exception:
        logger.exception("Could not record the split for bill %s", bill.pk)

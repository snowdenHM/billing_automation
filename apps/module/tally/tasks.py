# apps/module/tally/tasks.py
"""
Background tasks for Tally bill processing using Django-RQ.

Heavy lifting is delegated to ``apps.common.services.tasks`` — this module
only provides the thin Tally-specific orchestrators.
"""
import logging

from rq import get_current_job

from apps.common.services.tasks import (
    build_duplicate_metadata,
    enqueue_bill_processing,
    get_job_status,                   # re-exported for view imports
    mark_processing_done,
    mark_processing_error,
    mark_processing_start,
    update_bill_duplicate_fields,
)

from .models import TallyExpenseBill, TallyVendorBill

logger = logging.getLogger(__name__)

# Re-export so existing ``from ..tasks import get_job_status`` still works.
__all__ = [
    "enqueue_vendor_bill_processing",
    "enqueue_expense_bill_processing",
    "get_job_status",
    "process_vendor_bill_analysis",
    "process_expense_bill_analysis",
    "process_multiple_bills",
]


# ---------------------------------------------------------------------------
# Enqueue helpers
# ---------------------------------------------------------------------------

def enqueue_vendor_bill_processing(bill_id):
    """Enqueue a vendor bill for background processing."""
    return enqueue_bill_processing(process_vendor_bill_analysis, bill_id)


def enqueue_expense_bill_processing(bill_id):
    """Enqueue an expense bill for background processing."""
    return enqueue_bill_processing(process_expense_bill_analysis, bill_id)


def enqueue_pdf_split_vendor(bill_id):
    """Enqueue the page-by-page splitting of a placeholder vendor PDF."""
    return enqueue_bill_processing(split_pdf_bill_vendor, bill_id)


def enqueue_pdf_split_expense(bill_id):
    """Enqueue the page-by-page splitting of a placeholder expense PDF."""
    return enqueue_bill_processing(split_pdf_bill_expense, bill_id)


# ---------------------------------------------------------------------------
# PDF split task processors (async — see #14 in the upload audit)
# ---------------------------------------------------------------------------

def _split_pdf_placeholder(bill_id, *, model_class, enqueue_analysis_fn):
    """Render the placeholder's PDF into per-page bills.

    Called by ``django-rq`` from ``enqueue_pdf_split_*``. On success the
    placeholder bill is deleted and one new bill is created per page,
    each individually enqueued for AI analysis (mirrors what the
    inline path used to do — just off the request thread).
    """
    from io import BytesIO

    from apps.common.services.pdf_processing import split_pdf_to_bills

    try:
        placeholder = model_class.objects.get(id=bill_id)
    except model_class.DoesNotExist:
        logger.warning("PDF placeholder %s not found — skipping split", bill_id)
        return

    try:
        placeholder.file.seek(0)
        pdf_bytes = placeholder.file.read()
        # Wrap so ``split_pdf_to_bills`` can call ``.seek(0)`` / ``.read()``.
        buf = BytesIO(pdf_bytes)
        buf.name = placeholder.file.name
        page_bills = split_pdf_to_bills(
            buf,
            placeholder.organization,
            placeholder.file_type,
            placeholder.uploaded_by,
            model_class,
        )
        # Kick off analysis for each new per-page bill.
        for page_bill in page_bills:
            try:
                job = enqueue_analysis_fn(str(page_bill.id))
                page_bill.job_id = job.id
                page_bill.is_processing = True
                page_bill.save(update_fields=["job_id", "is_processing"])
            except Exception as enqueue_err:
                logger.error(
                    "Failed to enqueue analysis for split-page bill %s: %s",
                    page_bill.id, enqueue_err,
                )

        # Placeholder no longer needed — the per-page bills carry the data.
        try:
            placeholder.file.delete(save=False)
        except Exception:
            pass
        placeholder.delete()
        logger.info(
            "PDF split complete for placeholder %s: %d page-bills created",
            bill_id, len(page_bills),
        )
    except Exception as exc:
        logger.exception("PDF split failed for placeholder %s: %s", bill_id, exc)
        placeholder.is_processing = False
        placeholder.processing_error = f"PDF split failed: {exc}"
        placeholder.save(update_fields=["is_processing", "processing_error"])


def split_pdf_bill_vendor(bill_id, **kwargs):
    """RQ entry point: split a placeholder vendor PDF into per-page bills."""
    return _split_pdf_placeholder(
        bill_id,
        model_class=TallyVendorBill,
        enqueue_analysis_fn=enqueue_vendor_bill_processing,
    )


def split_pdf_bill_expense(bill_id, **kwargs):
    """RQ entry point: split a placeholder expense PDF into per-page bills."""
    return _split_pdf_placeholder(
        bill_id,
        model_class=TallyExpenseBill,
        enqueue_analysis_fn=enqueue_expense_bill_processing,
    )


# ---------------------------------------------------------------------------
# Task processors
# ---------------------------------------------------------------------------

def _process_tally_bill(bill_id, *, model_class, get_functions, bill_type):
    """
    Generic task body for Tally bill analysis + duplicate check.

    Parameters
    ----------
    model_class : TallyVendorBill or TallyExpenseBill
    get_functions : callable returning (analyze_fn, duplicate_fn)
    bill_type : str – 'vendor' or 'expense' (for logging)
    """
    job = get_current_job()
    try:
        bill = model_class.objects.get(id=bill_id)
        organization = bill.organization
        mark_processing_start(bill)
        logger.info("Starting background analysis for %s bill %s", bill_type, bill_id)

        analyze_fn, duplicate_fn = get_functions()

        # Step 1 — AI analysis
        try:
            analyze_fn(bill, organization)
            logger.info("AI analysis completed for %s bill %s", bill_type, bill_id)
        except Exception as exc:
            logger.error("AI analysis failed for %s bill %s: %s", bill_type, bill_id, exc)
            mark_processing_error(bill, f"AI analysis failed: {exc}")
            return

        # Step 2 — duplicate check
        try:
            result = duplicate_fn(bill, organization)
            update_bill_duplicate_fields(bill, result, include_url=True)
            logger.info("Duplicate check completed for %s bill %s", bill_type, bill_id)
        except Exception as exc:
            logger.error("Duplicate check failed for %s bill %s: %s", bill_type, bill_id, exc)
            bill.duplicate_description = f"Duplicate check failed: {exc}"
            bill.save(update_fields=["duplicate_description"])

        mark_processing_done(bill)
        logger.info("Background processing completed for %s bill %s", bill_type, bill_id)
        return f"Successfully processed {bill_type} bill {bill_id}"

    except model_class.DoesNotExist:
        logger.error("%s bill %s not found", bill_type.capitalize(), bill_id)
        raise
    except Exception as exc:
        logger.error("Background processing failed for %s bill %s: %s", bill_type, bill_id, exc)
        try:
            mark_processing_error(model_class.objects.get(id=bill_id), str(exc))
        except Exception:
            pass
        raise


def process_vendor_bill_analysis(bill_id, **kwargs):
    """Background task: AI-analyse a vendor bill, then check duplicates."""
    from .bill_processors import analyze_bill_with_ai, check_duplicate_tally_vendor_bill

    return _process_tally_bill(
        bill_id,
        model_class=TallyVendorBill,
        get_functions=lambda: (analyze_bill_with_ai, check_duplicate_tally_vendor_bill),
        bill_type="vendor",
    )


def process_expense_bill_analysis(bill_id, **kwargs):
    """Background task: AI-analyse an expense bill, then check duplicates."""
    from .bill_processors import analyze_expense_bill_with_ai, check_duplicate_tally_expense_bill

    return _process_tally_bill(
        bill_id,
        model_class=TallyExpenseBill,
        get_functions=lambda: (analyze_expense_bill_with_ai, check_duplicate_tally_expense_bill),
        bill_type="expense",
    )


# ---------------------------------------------------------------------------
# Batch helper
# ---------------------------------------------------------------------------

def process_multiple_bills(bill_ids, bill_type="vendor"):
    """Enqueue a batch of bills for background processing."""
    logger.info("Processing batch of %d %s bills", len(bill_ids), bill_type)
    fn = process_vendor_bill_analysis if bill_type == "vendor" else process_expense_bill_analysis
    results = []
    for bill_id in bill_ids:
        try:
            job = enqueue_bill_processing(fn, bill_id)
            results.append(f"Started processing {bill_type} bill {bill_id} — Job ID: {job.id}")
        except Exception as exc:
            logger.error("Failed to start processing %s bill %s: %s", bill_type, bill_id, exc)
            results.append(f"Failed to start processing {bill_type} bill {bill_id}: {exc}")
    return results
"""
Shared helpers for background bill-processing tasks (Django-RQ).

Every ``tasks.py`` module should delegate to these helpers instead of
duplicating enqueue / status / duplicate-metadata / error-handling logic.
"""

import logging

import django_rq
from rq.job import Job
from django_rq import get_connection

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enqueue / Job status
# ---------------------------------------------------------------------------

def enqueue_bill_processing(process_fn, *args, queue_name="default", timeout=600):
    """Enqueue *process_fn* on the given RQ queue and return the ``Job``."""
    queue = django_rq.get_queue(queue_name)
    job = queue.enqueue(process_fn, *args, timeout=timeout)
    logger.info("Enqueued %s — Job ID: %s", process_fn.__name__, job.id)
    return job


def get_job_status(job_id):
    """Return a dict describing the current state of an RQ job, or *None*."""
    try:
        connection = get_connection("default")
        job = Job.fetch(job_id, connection=connection)
        return {
            "id": job.id,
            "status": job.get_status(),
            "result": job.result,
            "exc_info": job.exc_info,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "ended_at": job.ended_at,
        }
    except Exception as exc:
        logger.error("Error fetching job %s: %s", job_id, exc)
        return None


# ---------------------------------------------------------------------------
# Processing-flag helpers
# ---------------------------------------------------------------------------

def mark_processing_start(bill):
    """Set ``is_processing=True`` and clear any previous error."""
    bill.is_processing = True
    bill.processing_error = ""
    bill.save(update_fields=["is_processing", "processing_error"])


def mark_processing_done(bill):
    """Clear the processing flag AND any stale error from a prior run.

    Without wiping ``processing_error`` here, a bill that once failed
    and later succeeded still carries the old message forever — which
    surfaces as a red "Error" badge on the list UI even though the
    row is now Synced/Analysed. Since we've reached the ``done`` state,
    the previous error is by definition no longer relevant.
    """
    bill.is_processing = False
    bill.processing_error = ""
    bill.save(update_fields=["is_processing", "processing_error"])


def mark_processing_error(bill, error_msg: str):
    """Record a processing error and clear the flag."""
    bill.is_processing = False
    bill.processing_error = str(error_msg)
    bill.save(update_fields=["is_processing", "processing_error"])


# ---------------------------------------------------------------------------
# Duplicate metadata
# ---------------------------------------------------------------------------

def build_duplicate_metadata(duplicate_bills, *, max_entries: int = 3, include_url: bool = False):
    """
    Turn the list returned by a ``check_duplicate_*`` function into a
    JSON-serialisable list of info dicts.

    Parameters
    ----------
    duplicate_bills : list[dict]
        Each dict must contain ``bill`` (model instance) and optionally
        ``invoice_number``, ``vendor_name``, ``total``, ``date``,
        ``similarity_score``.
    include_url : bool
        If *True*, attempt to build an absolute URL for the bill file.
    """
    entries = []
    for dup_data in duplicate_bills[:max_entries]:
        dup_bill = dup_data["bill"]
        info = {
            "bill_id": str(dup_bill.id),
            "bill_name": getattr(dup_bill, "bill_munshi_name", ""),
            "invoice_number": dup_data.get("invoice_number", "N/A"),
            "vendor_name": dup_data.get("vendor_name", "N/A"),
            "total": dup_data.get("total", 0),
            "date": dup_data.get("date", "N/A"),
            "similarity_score": dup_data.get("similarity_score", 0),
        }
        if include_url:
            info["bill_url"] = _bill_url(dup_bill)
        entries.append(info)
    return entries


def _bill_url(bill):
    """Best-effort absolute URL for a bill's file."""
    if not getattr(bill, "file", None):
        return None
    try:
        from django.conf import settings
        base = getattr(settings, "SITE_URL", "").rstrip("/")
        return f"{base}{bill.file.url}" if base else bill.file.url
    except Exception:
        return None


def update_bill_duplicate_fields(bill, duplicate_result, *, include_url: bool = False):
    """
    Process the ``(is_duplicate, duplicate_bills, similarity_score)`` tuple
    returned by a ``check_duplicate_*`` function and persist it on *bill*.
    """
    if not duplicate_result:
        return

    is_duplicate, duplicate_bills, similarity_score = duplicate_result

    bill.is_duplicate = is_duplicate
    bill.duplicate_score = similarity_score

    if is_duplicate:
        bill.duplicate_matched_bills = build_duplicate_metadata(
            duplicate_bills, include_url=include_url,
        )
        bill.duplicate_description = (
            f"Found {len(duplicate_bills)} potential duplicate(s) "
            f"with {similarity_score:.1f}% similarity"
        )
    else:
        bill.duplicate_description = "No duplicates found"
        bill.duplicate_matched_bills = []

    bill.save(update_fields=[
        "is_duplicate", "duplicate_score",
        "duplicate_matched_bills", "duplicate_description",
    ])

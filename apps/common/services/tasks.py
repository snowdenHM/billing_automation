"""
Shared helpers for background bill-processing tasks (Django-RQ).

Every ``tasks.py`` module should delegate to these helpers instead of
duplicating enqueue / status / duplicate-metadata / error-handling logic.
"""

import logging

import django_rq
from rq import Retry
from rq.job import Job
from django_rq import get_connection

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enqueue / Job status
# ---------------------------------------------------------------------------

# Keep finished/failed jobs around long enough that the status endpoint can
# still answer for a bill the user is looking at, and retry transient worker
# deaths (OOM on a big PDF, deploy restart) instead of leaving the bill stuck
# with is_processing=True.
JOB_RESULT_TTL = 24 * 3600
JOB_FAILURE_TTL = 7 * 24 * 3600
JOB_RETRY = Retry(max=2, interval=[60, 300])


def enqueue_bill_processing(process_fn, *args, queue_name="default", timeout=600):
    """Enqueue *process_fn* on the given RQ queue and return the ``Job``."""
    queue = django_rq.get_queue(queue_name)
    job = queue.enqueue(
        process_fn,
        *args,
        timeout=timeout,
        result_ttl=JOB_RESULT_TTL,
        failure_ttl=JOB_FAILURE_TTL,
        retry=JOB_RETRY,
    )
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

def _save_with_optional_stage(bill, fields, stage=None):
    """Save *fields*, adding ``processing_stage`` when the model has it.

    The Zoho bill models share these helpers but have no stage column, so
    the field is only written when it actually exists.
    """
    if stage is not None and hasattr(bill, "processing_stage"):
        bill.processing_stage = stage
        fields = [*fields, "processing_stage"]
    bill.save(update_fields=fields)


def mark_processing_stage(bill, stage):
    """Record which step of analysis *bill* has reached.

    Drives the progress bar on the bills list. Deliberately a bare save of
    one column — it is called between the slow steps of a job, so it must
    not become a reason for the job itself to fail.
    """
    if not hasattr(bill, "processing_stage"):
        return
    try:
        bill.processing_stage = stage
        bill.save(update_fields=["processing_stage"])
    except Exception:
        logger.exception("Could not record processing stage %s for bill %s", stage, bill.pk)


def mark_processing_start(bill):
    """Set ``is_processing=True`` and clear any previous error."""
    bill.is_processing = True
    bill.processing_error = ""
    _save_with_optional_stage(
        bill, ["is_processing", "processing_error"], stage="queued",
    )


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
    _save_with_optional_stage(
        bill, ["is_processing", "processing_error"], stage="done",
    )


def mark_processing_error(bill, error_msg: str):
    """Record a processing error and clear the flag."""
    bill.is_processing = False
    bill.processing_error = str(error_msg)
    _save_with_optional_stage(
        bill, ["is_processing", "processing_error"], stage="failed",
    )


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

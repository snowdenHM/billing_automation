# apps/module/zoho/tasks.py
"""
Background tasks for Zoho bill processing using Django-RQ.

Heavy lifting is delegated to ``apps.common.services.tasks`` — this module
only provides the thin Zoho-specific orchestrators.
"""
import logging

import django_rq

from apps.common.services.tasks import (
    enqueue_bill_processing,
    mark_processing_done,
    mark_processing_error,
    mark_processing_start,
    update_bill_duplicate_fields,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enqueue helpers
# ---------------------------------------------------------------------------

def enqueue_vendor_bill_analysis(bill_id, organization_id):
    """Enqueue vendor bill analysis task."""
    return enqueue_bill_processing(
        process_zoho_vendor_bill_analysis, bill_id, organization_id,
    )


def enqueue_expense_bill_analysis(bill_id, organization_id):
    """Enqueue expense bill analysis task."""
    return enqueue_bill_processing(
        process_zoho_expense_bill_analysis, bill_id, organization_id,
    )


def enqueue_journal_bill_analysis(bill_id, organization_id):
    """Enqueue journal bill analysis task."""
    return enqueue_bill_processing(
        process_zoho_journal_bill_analysis, bill_id, organization_id,
    )


# ---------------------------------------------------------------------------
# Async PDF-split task (see #14 in the upload audit)
# ---------------------------------------------------------------------------

def _split_zoho_pdf_placeholder(bill_id, *, model_class, enqueue_analysis_fn):
    from io import BytesIO

    from apps.common.services.pdf_processing import split_pdf_to_bills

    try:
        placeholder = model_class.objects.get(id=bill_id)
    except model_class.DoesNotExist:
        logger.warning("Zoho PDF placeholder %s not found", bill_id)
        return

    try:
        placeholder.file.seek(0)
        pdf_bytes = placeholder.file.read()
        buf = BytesIO(pdf_bytes)
        buf.name = placeholder.file.name
        page_bills = split_pdf_to_bills(
            buf,
            placeholder.organization,
            placeholder.fileType,
            placeholder.uploaded_by,
            model_class,
        )
        org_id_str = str(placeholder.organization.id)
        for page_bill in page_bills:
            try:
                job = enqueue_analysis_fn(str(page_bill.id), org_id_str)
                page_bill.job_id = job.id
                page_bill.is_processing = True
                page_bill.save(update_fields=["job_id", "is_processing"])
            except Exception as enqueue_err:
                logger.error(
                    "Failed to enqueue analysis for zoho page-bill %s: %s",
                    page_bill.id, enqueue_err,
                )
        try:
            placeholder.file.delete(save=False)
        except Exception:
            pass
        placeholder.delete()
        logger.info(
            "Zoho PDF split complete for %s: %d page-bills",
            bill_id, len(page_bills),
        )
    except Exception as exc:
        logger.exception("Zoho PDF split failed for %s: %s", bill_id, exc)
        placeholder.is_processing = False
        if hasattr(placeholder, "processing_error"):
            placeholder.processing_error = f"PDF split failed: {exc}"
            placeholder.save(update_fields=["is_processing", "processing_error"])
        else:
            placeholder.save(update_fields=["is_processing"])


def split_pdf_bill_vendor(bill_id, **kwargs):
    from .models import VendorBill
    return _split_zoho_pdf_placeholder(
        bill_id,
        model_class=VendorBill,
        enqueue_analysis_fn=enqueue_vendor_bill_analysis,
    )


def split_pdf_bill_expense(bill_id, **kwargs):
    from .models import ExpenseBill
    return _split_zoho_pdf_placeholder(
        bill_id,
        model_class=ExpenseBill,
        enqueue_analysis_fn=enqueue_expense_bill_analysis,
    )


def split_pdf_bill_journal(bill_id, **kwargs):
    from .models import JournalBill
    return _split_zoho_pdf_placeholder(
        bill_id,
        model_class=JournalBill,
        enqueue_analysis_fn=enqueue_journal_bill_analysis,
    )


def enqueue_pdf_split_vendor(bill_id):
    return enqueue_bill_processing(split_pdf_bill_vendor, bill_id)


def enqueue_pdf_split_expense(bill_id):
    return enqueue_bill_processing(split_pdf_bill_expense, bill_id)


def enqueue_pdf_split_journal(bill_id):
    return enqueue_bill_processing(split_pdf_bill_journal, bill_id)


# ---------------------------------------------------------------------------
# Internal: shared Zoho-specific task skeleton
# ---------------------------------------------------------------------------

def _process_zoho_bill(
    bill_id,
    organization_id,
    *,
    model_class,
    analysed_status,
    get_functions,
    bill_type: str,
    has_duplicate_check: bool = False,
):
    """
    Generic task body for all three Zoho bill types.

    Parameters
    ----------
    get_functions : callable
        A zero-arg function that lazily imports and returns the needed
        callables as a tuple:
        ``(analyze_fn, create_objects_fn)`` – or –
        ``(analyze_fn, create_objects_fn, duplicate_fn)`` when
        *has_duplicate_check* is True.
    """
    from apps.organizations.models import Organization

    try:
        bill = model_class.objects.get(id=bill_id)
        organization = Organization.objects.get(id=organization_id)
        mark_processing_start(bill)
        logger.info("Starting background processing for Zoho %s bill %s", bill_type, bill_id)

        # Step 1 — AI analysis + object creation
        try:
            fns = get_functions()
            analyze_fn, create_objects_fn = fns[0], fns[1]
            duplicate_fn = fns[2] if has_duplicate_check and len(fns) > 2 else None

            if not bill.file:
                raise Exception("No file attached to bill")

            file_content = bill.file.read()
            file_extension = bill.file.name.split(".")[-1].lower()

            logger.info("Running AI analysis for Zoho %s bill %s", bill_type, bill_id)
            analyzed_data = analyze_fn(file_content, file_extension)

            bill.analysed_data = analyzed_data
            bill.status = analysed_status
            bill.save(update_fields=["analysed_data", "status"])

            logger.info("Running automation for Zoho %s bill %s", bill_type, bill_id)
            create_objects_fn(bill, analyzed_data, organization)

        except Exception as exc:
            logger.error("AI analysis + automation failed for Zoho %s bill %s: %s", bill_type, bill_id, exc)
            mark_processing_error(bill, f"Analysis + automation failed: {exc}")
            # Reset status to Draft so user can retry
            bill.status = "Draft"
            bill.save(update_fields=["status"])
            # Re-raise to be handled by outer exception handler
            raise

        # Step 2 — optional duplicate check
        if has_duplicate_check and duplicate_fn:
            try:
                result = duplicate_fn(bill, organization)
                update_bill_duplicate_fields(bill, result)
                logger.info("Duplicate check completed for Zoho %s bill %s", bill_type, bill_id)
            except Exception as exc:
                logger.error("Duplicate check failed for Zoho %s bill %s: %s", bill_type, bill_id, exc)
                bill.duplicate_description = f"Duplicate check failed: {exc}"
                bill.save(update_fields=["duplicate_description"])

        mark_processing_done(bill)
        logger.info("Background processing completed for Zoho %s bill %s", bill_type, bill_id)
        return f"Successfully processed Zoho {bill_type} bill {bill_id}"

    except model_class.DoesNotExist:
        logger.error("Zoho %s bill %s not found", bill_type, bill_id)
        return f"Error: Zoho {bill_type} bill {bill_id} not found"
    except Exception as exc:
        logger.error("Background automation failed for Zoho %s bill %s: %s", bill_type, bill_id, exc)
        try:
            bill = model_class.objects.get(id=bill_id)
            mark_processing_error(bill, f"Automation failed: {exc}")
            # Reset status to Draft so user can retry
            bill.status = "Draft"
            bill.save(update_fields=["status"])
        except Exception:
            pass
        return f"Error: Background automation failed for Zoho {bill_type} bill {bill_id}: {exc}"


# ---------------------------------------------------------------------------
# Task processors
# ---------------------------------------------------------------------------

@django_rq.job("default", timeout=600)
def process_zoho_vendor_bill_analysis(bill_id, organization_id, **kwargs):
    """Background task: analyse Zoho vendor bill + create objects + duplicate check."""
    from .models import VendorBill

    def _fns():
        try:
            from .views.vendor_bills import (
                analyze_vendor_bill_with_openai,
                check_duplicate_bill,
                create_vendor_zoho_objects_from_analysis,
            )
        except (ImportError, ModuleNotFoundError) as e:
            logger.error("Sub-package import failed for vendor bill analysis: %s", e)
            # Fall back to flat views.py (server) or package __init__ (dev)
            try:
                from .views import (
                    analyze_vendor_bill_with_openai,
                    check_duplicate_bill,
                    create_vendor_zoho_objects_from_analysis,
                )
            except (ImportError, ModuleNotFoundError) as e2:
                logger.error("Fallback import also failed: %s", e2)
                raise
        return analyze_vendor_bill_with_openai, create_vendor_zoho_objects_from_analysis, check_duplicate_bill

    return _process_zoho_bill(
        bill_id, organization_id,
        model_class=VendorBill,
        analysed_status="Analysed",
        get_functions=_fns,
        bill_type="vendor",
        has_duplicate_check=True,
    )


@django_rq.job("default", timeout=600)
def process_zoho_expense_bill_analysis(bill_id, organization_id, **kwargs):
    """Background task: analyse Zoho expense bill + create objects + duplicate check."""
    from .models import ExpenseBill

    def _fns():
        try:
            from .views.expense_bills import (
                analyze_bill_with_openai,
                check_duplicate_expense_bill,
                create_expense_zoho_objects_from_analysis,
            )
        except (ImportError, ModuleNotFoundError) as e:
            logger.error("Sub-package import failed for expense bill analysis: %s", e)
            try:
                from .views import (
                    analyze_bill_with_openai,
                    check_duplicate_expense_bill,
                    create_expense_zoho_objects_from_analysis,
                )
            except (ImportError, ModuleNotFoundError) as e2:
                logger.error("Fallback import also failed: %s", e2)
                raise
        return analyze_bill_with_openai, create_expense_zoho_objects_from_analysis, check_duplicate_expense_bill

    return _process_zoho_bill(
        bill_id, organization_id,
        model_class=ExpenseBill,
        analysed_status="Analysed",
        get_functions=_fns,
        bill_type="expense",
        has_duplicate_check=True,
    )


@django_rq.job("default", timeout=600)
def process_zoho_journal_bill_analysis(bill_id, organization_id, **kwargs):
    """Background task: analyse Zoho journal bill + create objects + duplicate check."""
    from .models import JournalBill

    def _fns():
        try:
            from .views.journal_bills import (
                analyze_bill_with_openai,
                check_duplicate_journal_bill,
                create_journal_zoho_objects_from_analysis,
            )
        except (ImportError, ModuleNotFoundError) as e:
            logger.error("Sub-package import failed for journal bill analysis: %s", e)
            try:
                from .views import (
                    analyze_bill_with_openai,
                    check_duplicate_journal_bill,
                    create_journal_zoho_objects_from_analysis,
                )
            except (ImportError, ModuleNotFoundError) as e2:
                logger.error("Fallback import also failed: %s", e2)
                raise
        return analyze_bill_with_openai, create_journal_zoho_objects_from_analysis, check_duplicate_journal_bill

    return _process_zoho_bill(
        bill_id, organization_id,
        model_class=JournalBill,
        analysed_status="Analysed",
        get_functions=_fns,
        bill_type="journal",
        has_duplicate_check=True,
    )
# apps/module/tally/bill_processors.py
"""
Bill processing functions consumed by background RQ tasks.

Kept in a dedicated module that does NOT import from the ``views`` sub-package
so that task workers start cleanly whether ``views`` is:
  - a package directory (local dev):  apps/module/tally/views/
  - a flat module file  (server):      apps/module/tally/views.py

Whenever ``process_analysis_data`` / ``process_expense_analysis_data`` must be
called, a try/except import resolves the right location at runtime.
"""
import logging

from apps.common.services.bill_analysis import (
    analyze_bill_file,
    get_expense_bill_prompt,
    get_vendor_bill_prompt,
)
from apps.common.services.duplicate_detection import check_duplicate_bill

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers: resolve process_*_data functions at call-time
# ---------------------------------------------------------------------------

def _get_vendor_process_fn():
    """Return process_analysis_data regardless of whether views is a package."""
    try:
        from .views.vendor_bills import process_analysis_data
    except (ImportError, ModuleNotFoundError):
        from .views import process_analysis_data  # flat views.py on server
    return process_analysis_data


def _get_expense_process_fn():
    """Return process_expense_analysis_data regardless of views structure."""
    try:
        from .views.expense_bills import process_expense_analysis_data
    except (ImportError, ModuleNotFoundError):
        from .views import process_expense_analysis_data  # flat views.py on server
    return process_expense_analysis_data


# ---------------------------------------------------------------------------
# Duplicate-check wrappers (no views dependency at all)
# ---------------------------------------------------------------------------

def check_duplicate_tally_vendor_bill(bill, organization):
    """Delegate to apps.common.services.duplicate_detection.check_duplicate_bill."""
    from .models import TallyVendorBill
    return check_duplicate_bill(bill, organization, TallyVendorBill)


def check_duplicate_tally_expense_bill(bill, organization):
    """Delegate to apps.common.services.duplicate_detection.check_duplicate_bill."""
    from .models import TallyExpenseBill
    return check_duplicate_bill(bill, organization, TallyExpenseBill)


# ---------------------------------------------------------------------------
# AI-analysis wrappers
# ---------------------------------------------------------------------------

def analyze_bill_with_ai(bill, organization):
    """Analyze a vendor bill with AI and persist results.

    Returns a dict: ``{'success': bool, 'analyzed_bill': obj|None, 'error': str|None}``
    """
    logger.info("Starting AI analysis for bill %s, file: %s", bill.id, bill.file.name)

    try:
        json_data = analyze_bill_file(
            bill.file.path, bill.file.name, get_vendor_bill_prompt()
        )
        logger.info("AI analysis successful for bill %s", bill.id)
    except Exception as exc:
        logger.error("AI analysis failed for bill %s: %s", bill.id, exc)
        return {"success": False, "error": str(exc), "analyzed_bill": None}

    try:
        process_analysis_data = _get_vendor_process_fn()
        analyzed_bill = process_analysis_data(bill, json_data, organization)
        return {"success": True, "analyzed_bill": analyzed_bill, "error": None}
    except Exception as exc:
        logger.error("Error processing analysis data for bill %s: %s", bill.id, exc)
        return {
            "success": False,
            "error": f"Error processing analysis data: {exc}",
            "analyzed_bill": None,
        }


def analyze_expense_bill_with_ai(bill, organization):
    """Analyze an expense bill with AI and persist results.

    Raises on AI failure (matches the behaviour in views/expense_bills.py).
    """
    logger.info(
        "Starting AI analysis for expense bill %s, file: %s", bill.id, bill.file.name
    )

    try:
        json_data = analyze_bill_file(
            bill.file.path, bill.file.name, get_expense_bill_prompt()
        )
        logger.info("AI analysis successful for expense bill %s", bill.id)
    except Exception as exc:
        logger.error("AI analysis failed for expense bill %s: %s", bill.id, exc)
        raise Exception(f"AI processing failed: {exc}") from exc

    process_expense_analysis_data = _get_expense_process_fn()
    return process_expense_analysis_data(bill, json_data, organization)

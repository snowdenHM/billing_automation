"""
Generic duplicate detection logic for bills.

Works with any bill model that has:
  - ``analysed_data`` (JSONField)
  - ``organization`` (FK)
  - ``status`` field
  - ``is_duplicate``, ``duplicate_score``, ``duplicate_description``,
    ``duplicate_matched_bills`` fields for metadata storage.
"""
import logging
from contextlib import contextmanager

from django.db import connection, transaction

from apps.common.utils import calculate_string_similarity

logger = logging.getLogger(__name__)


# Arbitrary 32-bit namespace key for ``pg_try_advisory_xact_lock``.
# Keeps duplicate-check critical sections per (namespace, organization_id)
# so two concurrent RQ workers in the same org cannot race past the read
# of ``analysed_data`` before the other writes.
_DUPLICATE_LOCK_NAMESPACE = 0x6D616E61  # "mana"


@contextmanager
def _organization_dup_lock(organization_id):
    """Serialize duplicate-check critical sections per organization.

    On Postgres this uses a transaction-scoped advisory lock; on other DBs
    we fall back to a no-op (still wrapped in a transaction).
    """
    with transaction.atomic():
        if connection.vendor == "postgresql":
            try:
                org_id_int = int(str(organization_id).replace("-", "")[:8], 16)
            except (TypeError, ValueError):
                org_id_int = abs(hash(str(organization_id))) % (2 ** 31)
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(%s, %s)",
                    [_DUPLICATE_LOCK_NAMESPACE, org_id_int],
                )
        yield


def check_duplicate_bill(bill, organization, bill_model, analysed_statuses=None):
    """
    Check if a bill is a duplicate based on invoice number, vendor name,
    total amount, and date.

    Parameters
    ----------
    bill : Model instance
        The bill to check.
    organization : Organization
        The organization to scope the search.
    bill_model : Model class
        The Django model class to query for potential duplicates.
    analysed_statuses : list[str] | None
        List of status values to consider. Defaults to
        ``["Analysed", "Verified", "Synced"]``.

    Returns
    -------
    tuple[bool, list[dict], float]
        (is_duplicate, duplicate_bills, max_similarity_score)
    """
    if analysed_statuses is None:
        # Try to use model's BillStatus choices first
        if hasattr(bill_model, "BillStatus"):
            bs = bill_model.BillStatus
            analysed_statuses = [bs.ANALYSED, bs.VERIFIED, bs.SYNCED]
        else:
            analysed_statuses = ["Analysed", "Verified", "Synced"]

    logger.info(
        "Checking for duplicates of bill %s in organization %s",
        bill.id, organization.id,
    )

    if not bill.analysed_data:
        return False, [], 0.0

    analyzed_data = bill.analysed_data
    current_invoice_number = analyzed_data.get("invoiceNumber", "").strip()
    current_vendor_name = analyzed_data.get("from", {}).get("name", "").strip()
    current_total = analyzed_data.get("total", 0)
    current_date = analyzed_data.get("dateIssued", "")

    if not current_invoice_number and not current_vendor_name:
        return False, [], 0.0

    # Serialize per-organization so two concurrent uploads of the same bill
    # cannot both pass this check before either marks itself as a duplicate.
    duplicate_bills = []
    max_similarity = 0.0

    with _organization_dup_lock(organization.id):
        potential_duplicates = list(
            bill_model.objects.filter(
                organization=organization,
                status__in=analysed_statuses,
            ).exclude(id=bill.id)
        )

        for other_bill in potential_duplicates:
            if not other_bill.analysed_data:
                continue

            other_data = other_bill.analysed_data
            other_invoice_number = other_data.get("invoiceNumber", "").strip()
            other_vendor_name = other_data.get("from", {}).get("name", "").strip()
            other_total = other_data.get("total", 0)
            other_date = other_data.get("dateIssued", "")

            similarity_score = 0.0
            match_reasons = []

            # Invoice number match (40 pts)
            if (
                current_invoice_number
                and other_invoice_number
                and current_invoice_number.lower() == other_invoice_number.lower()
            ):
                similarity_score += 40.0
                match_reasons.append("exact_invoice_number")

            # Vendor name similarity (25 pts)
            if current_vendor_name and other_vendor_name:
                vendor_sim = calculate_string_similarity(
                    current_vendor_name.lower(), other_vendor_name.lower()
                )
                if vendor_sim > 0.8:
                    similarity_score += 25.0 * vendor_sim
                    match_reasons.append("vendor_name_match")

            # Total amount match (20 pts exact / 10 pts close / -20 pts conflict).
            # A conflicting amount is strong evidence the two bills are
            # NOT the same — even if invoice numbers coincidentally match
            # (annual counter reset, OCR misread). Penalising here prevents
            # a false-positive "duplicate" flag from triggering on same
            # vendor + same invoice number + different amount.
            if current_total and other_total:
                try:
                    cur_amt = float(current_total)
                    oth_amt = float(other_total)
                    if abs(cur_amt - oth_amt) < 0.01:
                        similarity_score += 20.0
                        match_reasons.append("exact_amount")
                    elif max(cur_amt, oth_amt) > 0 and abs(cur_amt - oth_amt) / max(cur_amt, oth_amt) < 0.05:
                        similarity_score += 10.0
                        match_reasons.append("similar_amount")
                    else:
                        similarity_score -= 20.0
                        match_reasons.append("conflicting_amount")
                except (ValueError, TypeError):
                    pass

            # Date match (15 pts / -10 pts conflict).
            if current_date and other_date:
                if current_date == other_date:
                    similarity_score += 15.0
                    match_reasons.append("same_date")
                else:
                    similarity_score -= 10.0
                    match_reasons.append("conflicting_date")

            if similarity_score >= 60.0:
                duplicate_bills.append({
                    "bill": other_bill,
                    "similarity_score": similarity_score,
                    "match_reasons": match_reasons,
                    "invoice_number": other_invoice_number,
                    "vendor_name": other_vendor_name,
                    "total": other_total,
                    "date": other_date,
                })
                max_similarity = max(max_similarity, similarity_score)

    is_duplicate = len(duplicate_bills) > 0
    logger.info(
        "Duplicate check complete. Found %d potential duplicates (max similarity %.1f)",
        len(duplicate_bills), max_similarity,
    )
    return is_duplicate, duplicate_bills, max_similarity


def update_bill_duplicate_metadata(bill, duplicate_bills, max_similarity):
    """
    Persist duplicate detection metadata on a bill instance.

    Works with any bill model that has the standard duplicate tracking fields:
    ``is_duplicate``, ``duplicate_score``, ``duplicate_description``,
    ``duplicate_matched_bills``.
    """
    try:
        duplicate_bills = duplicate_bills or []
        max_similarity = max_similarity or 0.0

        # Signed URLs so the file viewer can load these matches even
        # though ``/media/bills/…`` refuses unsigned requests.
        from apps.common.views import generate_signed_bill_file_url
        formatted_matches = []
        for dup in duplicate_bills[:5]:
            dup_bill = dup.get("bill")
            file_url = None
            try:
                if dup_bill and dup_bill.file:
                    file_url = generate_signed_bill_file_url(dup_bill.file)
            except Exception:
                file_url = None

            formatted_matches.append({
                "bill_id": str(getattr(dup_bill, "id", "")),
                "bill_name": getattr(dup_bill, "bill_munshi_name", "")
                             or getattr(dup_bill, "billmunshiName", ""),
                "status": getattr(dup_bill, "status", ""),
                "invoice_number": dup.get("invoice_number"),
                "vendor_name": dup.get("vendor_name"),
                "total": dup.get("total"),
                "date": dup.get("date"),
                "similarity_score": round(float(dup.get("similarity_score", 0.0)), 2),
                "match_reasons": dup.get("match_reasons", []),
                "file_url": file_url,
            })

        bill.is_duplicate = bool(formatted_matches)
        bill.duplicate_score = round(float(max_similarity), 2)
        bill.duplicate_matched_bills = formatted_matches
        bill.duplicate_description = (
            f"Found {len(formatted_matches)} potential duplicate(s) "
            f"with {bill.duplicate_score:.1f}% similarity"
            if formatted_matches
            else "No duplicates found during latest analysis"
        )
        bill.save(update_fields=[
            "is_duplicate",
            "duplicate_score",
            "duplicate_description",
            "duplicate_matched_bills",
        ])
    except Exception as exc:
        logger.exception(
            "Failed to update duplicate metadata for bill %s: %s",
            getattr(bill, "id", "unknown"), exc,
        )

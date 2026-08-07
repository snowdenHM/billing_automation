"""Trash registry and purge helpers for Tally bills.

Deleting a bill from a list page moves it here rather than destroying it.
It stays restorable for ``TRASH_RETENTION_DAYS`` (30 by default), after
which ``purge_trashed_bills`` destroys the row and its uploaded file.

Both the API views and the purge command import from this module so the
notion of "what is trashable" and "how a purge is performed" is defined
exactly once.
"""

import logging

from apps.common.models import get_trash_retention_days, trash_cutoff
from apps.module.tally.models import (
    TallyExpenseBill,
    TallyPaymentBill,
    TallyVendorBill,
)

logger = logging.getLogger(__name__)


class TrashKind:
    """One trashable document type.

    ``slug`` is what appears in trash URLs and in the API payload; it is
    deliberately the user-facing name of the page the bill came from
    rather than the model name, so the Trash UI can group and link back
    without translating model names itself.
    """

    def __init__(self, slug, model, label, list_path):
        self.slug = slug
        self.model = model
        self.label = label
        self.list_path = list_path

    def __repr__(self):  # pragma: no cover
        return f"<TrashKind {self.slug}>"


#: Keyed by URL slug. Adding a trashable model is a matter of adding a row
#: here — the list, restore, purge and command paths all iterate this.
TRASH_KINDS = {
    "purchase-voucher": TrashKind(
        "purchase-voucher", TallyVendorBill, "Purchase Voucher", "/tally/vendor-bill"
    ),
    "journal-entry": TrashKind(
        "journal-entry", TallyExpenseBill, "Journal Entry", "/tally/expense-bill"
    ),
    "payment-voucher": TrashKind(
        "payment-voucher", TallyPaymentBill, "Payment Voucher", "/tally/payment-voucher"
    ),
}


def get_kind(slug):
    """Return the :class:`TrashKind` for *slug*, or ``None`` if unknown."""
    return TRASH_KINDS.get((slug or "").strip().lower())


#: Shown to the user, and returned by the API, when a delete is refused.
SYNCED_BLOCK_MESSAGE = (
    "This bill has already been posted to Tally and can no longer be deleted."
)


def trash_blocked_reason(bill):
    """Why *bill* may not be moved to trash, or ``None`` if it may.

    A bill that has genuinely reached Tally is a posted accounting record —
    deleting it here would leave the two systems disagreeing, so it is
    refused outright.

    The check is deliberately ``status`` **and** ``tally_synced`` rather
    than status alone. A bill can sit at ``Synced`` while the push to Tally
    never actually landed; ``vendor_bills_sync_list`` still offers exactly
    those bills for re-sync. Blocking on status alone would strand a failed
    sync as an undeletable row nobody can clear.
    """
    status_value = getattr(bill, "status", None)
    model = type(bill)
    synced_status = getattr(model, "BillStatus", None)
    synced_value = getattr(synced_status, "SYNCED", "Synced")

    if status_value == synced_value and getattr(bill, "tally_synced", False):
        return SYNCED_BLOCK_MESSAGE
    return None


def can_be_trashed(bill):
    """Whether *bill* may be moved to trash."""
    return trash_blocked_reason(bill) is None


def purge_bill(bill):
    """Destroy a trashed bill permanently: its file first, then the row.

    The file is removed through the storage backend rather than by path
    manipulation, so this stays correct if media ever moves off local
    disk. A file that is already missing is not an error — the row still
    needs to go, and refusing to delete it would leave an undeletable
    entry stuck in the trash forever.
    """
    file_field = getattr(bill, "file", None)
    if file_field:
        try:
            file_field.delete(save=False)
        except Exception as exc:  # pragma: no cover - storage-dependent
            logger.warning(
                "Trash purge: could not delete file for bill %s (%s): %s",
                bill.pk, type(bill).__name__, exc,
            )

    bill.delete()


def purge_expired(now=None, kinds=None, dry_run=False):
    """Purge every trashed bill past its retention window.

    Args:
        now: Override for the current time (tests / backdated runs).
        kinds: Restrict to these :class:`TrashKind` values; defaults to all.
        dry_run: Count what would be destroyed without destroying it.

    Returns:
        dict of ``{kind_slug: purged_count}``.
    """
    results = {}

    for kind in (kinds if kinds is not None else TRASH_KINDS.values()):
        # Re-evaluated per kind so a long run still measures each model
        # against the same cutoff instant passed in by the caller.
        expired = kind.model.objects.expired(now=now)

        if dry_run:
            results[kind.slug] = expired.count()
            continue

        purged = 0
        # Iterate concrete instances — purge_bill needs the file field, so
        # a bulk delete() would orphan every uploaded file on disk.
        for bill in expired.iterator():
            try:
                purge_bill(bill)
                purged += 1
            except Exception as exc:
                logger.exception(
                    "Trash purge: failed to purge %s %s: %s", kind.slug, bill.pk, exc
                )

        results[kind.slug] = purged

    return results


def retention_summary():
    """Small dict describing the policy, echoed to the Trash UI."""
    return {
        "retention_days": get_trash_retention_days(),
        "cutoff": trash_cutoff(),
    }

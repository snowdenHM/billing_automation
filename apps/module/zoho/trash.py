"""Trash registry and purge helpers for Zoho bills.

The mirror image of :mod:`apps.module.tally.trash`. Deleting a bill from a
Zoho list page moves it here rather than destroying it; it stays
restorable for ``TRASH_RETENTION_DAYS`` (30 by default), after which
``purge_expired`` destroys the row and its uploaded file.

Why a separate registry rather than rows added to Tally's ``TRASH_KINDS``:
the two modules share the *mechanism* (``TrashableMixin`` in
``apps.common.models``) but not the *policy*. "Has this been posted?" is
answered from different columns on each side, the slugs would collide
("purchase-voucher" exists in both), and a Tally module importing Zoho
models to serve a Zoho list is a dependency neither module wants. Each
module therefore owns its own registry and its own four endpoints, and
the Trash screen merges them.
"""

import logging

from apps.common.models import get_trash_retention_days, trash_cutoff
from apps.module.zoho.models import (
    ExpenseBill,
    JournalBill,
    PaymentBill,
    ReceiptBill,
    SalesBill,
    VendorBill,
)

logger = logging.getLogger(__name__)

#: Which module these kinds belong to. Echoed in every payload so the
#: Trash screen can tell a Zoho purchase voucher from a Tally one.
MODULE = "zoho"
MODULE_LABEL = "Zoho"


class TrashKind:
    """One trashable Zoho document type.

    ``slug`` is what appears in trash URLs and in the API payload; it is
    the user-facing name of the page the bill came from rather than the
    model name, so the Trash UI can group and link back without
    translating model names itself.

    ``remote_id_field`` names the column that holds the Zoho Books
    document id once the push has landed, or ``None`` for the two kinds
    Zoho gives us no id back for. See :func:`trash_blocked_reason`.
    """

    def __init__(self, slug, model, label, list_path, remote_id_field=None):
        self.slug = slug
        self.model = model
        self.label = label
        self.list_path = list_path
        self.remote_id_field = remote_id_field

    def __repr__(self):  # pragma: no cover
        return f"<ZohoTrashKind {self.slug}>"


#: Keyed by URL slug. Adding a trashable model is a matter of adding a row
#: here — the list, restore, purge and command paths all iterate this.
TRASH_KINDS = {
    "purchase-voucher": TrashKind(
        "purchase-voucher", VendorBill, "Purchase Voucher",
        "/zoho/vendor-bill", remote_id_field="zoho_bill_id",
    ),
    "journal-entry": TrashKind(
        "journal-entry", JournalBill, "Journal Entry",
        "/zoho/journal-entry",
    ),
    "expense-bill": TrashKind(
        "expense-bill", ExpenseBill, "Expense Bill",
        "/zoho/expense-bill",
    ),
    "sales-voucher": TrashKind(
        "sales-voucher", SalesBill, "Sales Voucher",
        "/zoho/sales-bill", remote_id_field="zoho_invoice_id",
    ),
    "receipt-voucher": TrashKind(
        "receipt-voucher", ReceiptBill, "Receipt Voucher",
        "/zoho/receipt-voucher", remote_id_field="zoho_payment_id",
    ),
    "payment-voucher": TrashKind(
        "payment-voucher", PaymentBill, "Payment Voucher",
        "/zoho/payment-voucher", remote_id_field="zoho_payment_id",
    ),
}

#: Reverse lookup, so a view holding only a model can name its kind.
KIND_BY_MODEL = {kind.model: kind for kind in TRASH_KINDS.values()}


def get_kind(slug):
    """Return the :class:`TrashKind` for *slug*, or ``None`` if unknown."""
    return TRASH_KINDS.get((slug or "").strip().lower())


def kind_for_model(model):
    """Return the :class:`TrashKind` for a bill model, or ``None``."""
    return KIND_BY_MODEL.get(model)


#: Shown to the user, and returned by the API, when a delete is refused.
SYNCED_BLOCK_MESSAGE = (
    "This bill has already been posted to Zoho Books and can no longer be deleted."
)

#: Any remote-id column a Zoho bill model might carry. Checked by name so
#: the rule holds for a model whose kind is not in the registry.
REMOTE_ID_FIELDS = ("zoho_bill_id", "zoho_invoice_id", "zoho_payment_id")

SYNCED_STATUS = "Synced"


def trash_blocked_reason(bill):
    """Why *bill* may not be moved to trash, or ``None`` if it may.

    A bill that has reached Zoho Books is a posted accounting record. A
    local delete cannot unmake an invoice, a journal or a payment over
    there, so deleting it here would leave the two systems disagreeing
    and is refused outright — the same rule Tally enforces.

    **The test is the opposite shape to Tally's, deliberately.** Tally
    requires ``status == Synced`` *and* ``tally_synced``, because Tally's
    push is fire-and-forget at a local connector: a bill can sit at
    ``Synced`` while nothing ever landed, and blocking on status alone
    would strand a failed sync as an undeletable row.

    Zoho answers synchronously. Every Zoho sync view sets
    ``status = 'Synced'`` only inside the ``201 Created`` branch, after
    the remote document exists — so here ``status`` is not a hopeful
    intention, it is a receipt. The id columns (``zoho_bill_id``,
    ``zoho_invoice_id``, ``zoho_payment_id``) are the same evidence read
    from the other side, and only four of the six models have one:
    expenses and journals get no id persisted at all.

    Both signals are therefore positive evidence and the test is an OR,
    not an AND. A row carrying either must not be deleted. ORing also
    closes the gap where the POST returned 201 but the ``save()`` that
    followed did not commit the status.
    """
    if getattr(bill, "status", None) == SYNCED_STATUS:
        return SYNCED_BLOCK_MESSAGE

    for field in REMOTE_ID_FIELDS:
        if getattr(bill, field, None):
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
                "Zoho trash purge: could not delete file for bill %s (%s): %s",
                bill.pk, type(bill).__name__, exc,
            )

    bill.delete()


def purge_expired(now=None, kinds=None, dry_run=False):
    """Purge every trashed Zoho bill past its retention window.

    Args:
        now: Override for the current time (tests / backdated runs).
        kinds: Restrict to these :class:`TrashKind` values; defaults to all.
        dry_run: Count what would be destroyed without destroying it.

    Returns:
        dict of ``{kind_slug: purged_count}``.
    """
    results = {}

    for kind in (kinds if kinds is not None else TRASH_KINDS.values()):
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
                    "Zoho trash purge: failed to purge %s %s: %s", kind.slug, bill.pk, exc
                )

        results[kind.slug] = purged

    return results


def retention_summary():
    """Small dict describing the policy, echoed to the Trash UI."""
    return {
        "retention_days": get_trash_retention_days(),
        "cutoff": trash_cutoff(),
    }

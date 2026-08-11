# apps/module/tally/signals.py
"""
Onboarding defaults for TallyConfig.

Client Correction 16: when a new TallyConfig row is created (i.e. a new
client/organization is being onboarded onto the Tally integration), the
standard Tally parent-ledger mappings should be auto-filled instead of
starting blank. The user can still change any of these later from the
Tally config page.

Defaults (per client requirement):
    IGST / CGST / SGST / TDS parents -> "Duties & Taxes"
    Vendor ledger parents            -> "Sundry Creditors"
    Purchase ledger parents          -> "Purchase Accounts", "Direct Expenses"
    Expense ledger parents           -> "Direct Expenses", "Indirect Expenses"
    Payment ledger parents           -> "Bank Accounts", "Cash-in-Hand"
    Cess / Discount / Freight /
    Round Off parents                -> "Indirect Expenses"
"""
import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)

# field name on TallyConfig -> default ParentLedger name(s) to seed it with.
DEFAULT_TALLY_CONFIG_PARENTS = {
    "igst_parents": ["Duties & Taxes"],
    "cgst_parents": ["Duties & Taxes"],
    "sgst_parents": ["Duties & Taxes"],
    "tds_parents": ["Duties & Taxes"],
    "vendor_parents": ["Sundry Creditors"],
    "chart_of_accounts_parents": ["Purchase Accounts", "Direct Expenses"],
    "chart_of_accounts_expense_parents": ["Direct Expenses", "Indirect Expenses"],
    "payment_parents": ["Bank Accounts", "Cash-in-Hand"],
    "cess_parents": ["Indirect Expenses"],
    "discount_parents": ["Indirect Expenses"],
    "freight_parents": ["Indirect Expenses"],
    "round_off_parents": ["Indirect Expenses"],
}


@receiver(post_save, sender="tally.TallyConfig", dispatch_uid="tally_seed_default_config_parents")
def seed_default_tally_config_parents(sender, instance, created, **kwargs):
    """Auto-fill standard parent-ledger mappings for a brand-new TallyConfig.

    Only runs on creation, and only touches fields that are still empty, so
    it never clobbers a mapping the user (or an earlier signal run) already
    set explicitly.
    """
    if not created:
        return

    from .models import ParentLedger, SyncSource

    organization = instance.organization
    if organization is None:
        return

    try:
        for field_name, parent_names in DEFAULT_TALLY_CONFIG_PARENTS.items():
            manager = getattr(instance, field_name, None)
            if manager is None:
                continue
            if manager.exists():
                # Already populated (e.g. explicitly passed in on create) — don't override.
                continue

            parents = []
            for parent_name in parent_names:
                parent_ledger, _ = ParentLedger.objects.get_or_create(
                    organization=organization,
                    parent=parent_name,
                    defaults={"source": SyncSource.BILLMUNSHI},
                )
                parents.append(parent_ledger)

            if parents:
                manager.set(parents)
    except Exception:
        # Never break TallyConfig creation because of onboarding defaults.
        logger.exception(
            "Failed to seed default TallyConfig parent ledgers for organization %s",
            getattr(organization, "id", None),
        )

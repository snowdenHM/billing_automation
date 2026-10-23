"""Reverse charge on Journal and Payment vouchers.

Under RCM the recipient pays the GST instead of the supplier, so the
same tax figure belongs on the voucher twice: once as input credit, and
once as the liability owed to the government. In Tally that is two
ledger lines of equal amount and opposite sign:

    Expense                  Dr   50,000
    CGST (Input, RCM)        Dr    4,500
    SGST (Input, RCM)        Dr    4,500
    CGST (RCM Payable)       Cr    4,500
    SGST (RCM Payable)       Cr    4,500
    Sundry Creditor          Cr   50,000

The debit rows are the ordinary GST lines an operator already fills in.
This module builds the credit rows from them, so turning reverse charge
on is one switch rather than four more dropdowns to get right.

**The sync payload does not change.** Both stacks already emit one
``<ledger>`` per GST line with its own ``<debit_or_credit>``, so the RCM
rows arrive as more of the rows the connector already handles. Nothing
needs doing on the Tally side.

Off unless the operator turns it on. ``is_reverse_charge`` defaults to
False on every bill, and with it off this module makes no changes and
removes nothing.
"""

import logging
from decimal import Decimal

from django.db import transaction

logger = logging.getLogger(__name__)

#: Marks a GST line this module created, so a later run can tell its own
#: rows from the ones a person entered and never overwrite theirs.
RCM_RATE_MARKER = "RCM"


def _gst_line_model(analyzed_bill):
    """The GstLine model for whichever stack *analyzed_bill* belongs to."""
    from .models import TallyExpenseGstLine, TallyPaymentGstLine

    return {
        "TallyExpenseAnalyzedBill": (TallyExpenseGstLine, "expense_bill"),
        "TallyPaymentAnalyzedBill": (TallyPaymentGstLine, "payment_bill"),
    }.get(type(analyzed_bill).__name__, (None, None))


def rcm_ledger_for(organization, tax_type):
    """The RCM payable ledger for *tax_type*, or None.

    Scoped to ``TallyConfig.rcm_parents``, which is deliberately a group
    of its own: the input-credit ledger and the payable ledger are
    different accounts even though both sit under Duties & Taxes, and
    posting the liability to the credit ledger would cancel the credit
    out instead of recording it.

    Picks the ledger whose name mentions the tax type, falling back to
    the first in the group so a single combined RCM ledger also works.
    """
    from .models import Ledger, TallyConfig

    config = TallyConfig.objects.filter(organization=organization).first()
    if config is None:
        return None
    parents = config.rcm_parents.all()
    if not parents.exists():
        return None

    candidates = Ledger.objects.filter(
        organization=organization, parent__in=parents,
    ).order_by("name")
    needle = (tax_type or "").lower()
    for ledger in candidates:
        if needle and needle in (ledger.name or "").lower():
            return ledger
    return candidates.first()


def _is_rcm_line(line):
    return (line.rate or "").startswith(RCM_RATE_MARKER)


@transaction.atomic
def sync_rcm_lines(analyzed_bill, organization=None):
    """Bring the RCM credit lines in step with the bill's GST lines.

    Returns ``(created, removed)``. Idempotent: run it after every save
    and the bill converges on the right set of rows rather than
    accumulating duplicates.

    With reverse charge off, every row this module previously created is
    removed and nothing else is touched — switching the flag back is a
    clean undo, not a half-state the operator has to clear by hand.
    """
    model, fk_name = _gst_line_model(analyzed_bill)
    if model is None:
        return 0, 0

    org = organization or getattr(analyzed_bill, "organization", None)
    existing = list(model.objects.filter(**{fk_name: analyzed_bill}))
    ours = [line for line in existing if _is_rcm_line(line)]

    if not getattr(analyzed_bill, "is_reverse_charge", False):
        removed = len(ours)
        if removed:
            model.objects.filter(pk__in=[line.pk for line in ours]).delete()
        return 0, removed

    # One credit row per input row, matched on tax type and amount so a
    # mixed-rate bill produces a mirror for each rate rather than one
    # lump that no single rate explains.
    inputs = [
        line for line in existing
        if not _is_rcm_line(line) and _money(line.amount) != Decimal("0")
    ]

    wanted = {}
    for line in inputs:
        key = (line.tax_type, str(_money(line.amount)), line.rate or "")
        wanted[key] = wanted.get(key, 0) + 1

    have = {}
    for line in ours:
        key = (line.tax_type, str(_money(line.amount)), _source_rate(line))
        have[key] = have.get(key, 0) + 1

    created = 0
    for (tax_type, amount, rate), count in wanted.items():
        shortfall = count - have.get((tax_type, amount, rate), 0)
        for _ in range(max(0, shortfall)):
            ledger = rcm_ledger_for(org, tax_type)
            if ledger is None:
                logger.warning(
                    "Reverse charge on bill %s: no RCM payable ledger configured "
                    "for %s — the liability line was not created.",
                    getattr(analyzed_bill, "pk", "?"), tax_type,
                )
                continue
            model.objects.create(
                organization=org,
                **{fk_name: analyzed_bill},
                tax_type=tax_type,
                amount=Decimal(amount),
                rate=_rcm_rate(rate),
                ledger=ledger,
                debit_or_credit=model.DebitCredit.CREDIT,
            )
            created += 1

    # Anything of ours the inputs no longer justify — the operator
    # changed an amount, or deleted a line.
    surplus_pks = []
    for line in ours:
        key = (line.tax_type, str(_money(line.amount)), _source_rate(line))
        if have.get(key, 0) > wanted.get(key, 0):
            have[key] -= 1
            surplus_pks.append(line.pk)
    removed = len(surplus_pks)
    if surplus_pks:
        model.objects.filter(pk__in=surplus_pks).delete()

    if created or removed:
        logger.info(
            "Reverse charge on bill %s: %d liability line(s) added, %d removed.",
            getattr(analyzed_bill, "pk", "?"), created, removed,
        )
    return created, removed


def _rcm_rate(source_rate):
    """The ``rate`` string stored on a line this module created.

    Carries the source rate so the pairing survives a mixed-rate bill,
    and the marker so the row is recognisably ours.
    """
    return f"{RCM_RATE_MARKER} {source_rate}".strip()


def _source_rate(line):
    """The rate an RCM line was built from."""
    rate = (line.rate or "")
    return rate[len(RCM_RATE_MARKER):].strip() if _is_rcm_line(line) else rate


def _money(value):
    if value in (None, ""):
        return Decimal("0.00")
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except Exception:
        return Decimal("0.00")

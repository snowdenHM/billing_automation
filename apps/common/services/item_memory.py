"""Remember which stock item a vendor's wording means.

Item auto-match compared the vendor's description to the Tally item
name, exactly. That almost never matches — "Sadabahar Soyabadi 20 Kg"
has to become "FORTUNE SOYA 15LTR" — so the item dropdown was a manual
pick on essentially every line of every bill.

The operator already answers that question when they verify. This stores
the answer and offers it next time. No model, no fuzzy matching, no risk
of inventing a mapping nobody asked for: every entry here was taught by
a person for that exact vendor and that exact wording.

Two rules keep it safe:

* **Scoped to the party.** Two suppliers use the same words for
  different goods, and a mapping learned from one must never reach
  another's bill. The organisation scope is on top of that, so one
  client's mappings never reach another client.

  "Party" is the ledger on the voucher: the vendor on a purchase, the
  customer on a sale. Both are ordinary ``Ledger`` rows, so both use
  the same table. The column is still called ``vendor`` because that is
  what it was called when only purchases used it, and renaming it would
  buy nothing a migration's worth of risk.

* **Exact on the normalised key, never fuzzy.** A wrong auto-filled item
  is worse than an empty one: it posts the wrong stock movement, and
  nobody re-reads a field that already looks filled. Normalisation
  absorbs case, punctuation and spacing — nothing else.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_PUNCT = re.compile(r"[^a-z0-9]+")
#: Splits a digit/letter boundary so "20KG" and "20 kg" normalise the
#: same. Without it the two spellings of the same pack size looked like
#: different items, which is exactly the variation this has to absorb.
_DIGIT_LETTER = re.compile(r"(?<=\d)(?=[a-z])|(?<=[a-z])(?=\d)")


def normalise_description(text) -> str:
    """Lowercase, strip punctuation, collapse whitespace.

    Deliberately conservative. It absorbs the ways the same description
    gets typed differently ("POHA 20KG", "poha - 20 kg") without
    absorbing a genuine difference ("20 Kg" vs "50 Kg" stay distinct,
    because the digits survive).
    """
    lowered = str(text or "").lower()
    spaced = _DIGIT_LETTER.sub(" ", lowered)
    return " ".join(_PUNCT.sub(" ", spaced).split())


def remember_item_choice(*, organization, party, description,
                         stock_item=None, ledger=None):
    """Record what *party* calling something *description* turned out to mean.

    Two answers, either of which may be absent: the stock item, and the
    line ledger it was posted to. A line can have a ledger and no stock
    item (inventory sync off) or a stock item and no ledger, and both
    are worth remembering on their own.

    Called when an operator verifies a bill. Never raises: failing to
    learn is not a reason to fail a verify the user asked for.
    """
    key = normalise_description(description)
    if not (organization and party and key):
        return None
    if stock_item is None and ledger is None:
        return None

    try:
        from django.db.models import F
        from apps.module.tally.models import ItemDescriptionMapping

        row, created = ItemDescriptionMapping.objects.get_or_create(
            organization=organization,
            vendor=party,
            description_key=key[:255],
            defaults={
                "stock_item": stock_item,
                "ledger": ledger,
                "description_sample": str(description)[:255],
            },
        )
        if not created:
            # The operator has changed their mind about this wording —
            # the newest answer wins, because they are looking at the
            # bill and we are not.
            #
            # Only the halves this call actually carries are written. A
            # verify that resolved the ledger but not the stock item
            # must not erase a stock item taught last month.
            updates = {"hit_count": F("hit_count") + 1,
                       "description_sample": str(description)[:255]}
            if stock_item is not None and row.stock_item_id != stock_item.id:
                updates["stock_item"] = stock_item
            if ledger is not None and row.ledger_id != ledger.id:
                updates["ledger"] = ledger
            ItemDescriptionMapping.objects.filter(pk=row.pk).update(**updates)
        return row
    except Exception as exc:
        logger.warning("Could not remember item mapping (%s)", exc)
        return None


def recall_item_choices(*, organization, party, descriptions):
    """``{normalised description: ItemDescriptionMapping}`` for what we know.

    The row rather than the stock item, because a mapping now carries
    two answers — the item and the line ledger — and a caller generally
    wants both. ``row.stock_item`` and ``row.ledger`` are each nullable.

    One query for the whole bill rather than one per line.
    """
    keys = {normalise_description(d) for d in (descriptions or [])}
    keys.discard("")
    if not (organization and party and keys):
        return {}

    try:
        from apps.module.tally.models import ItemDescriptionMapping

        rows = (
            ItemDescriptionMapping.objects
            .filter(organization=organization, vendor=party,
                    description_key__in=keys)
            .select_related("stock_item", "ledger")
        )
        return {row.description_key: row for row in rows}
    except Exception as exc:
        logger.warning("Could not recall item mappings (%s)", exc)
        return {}

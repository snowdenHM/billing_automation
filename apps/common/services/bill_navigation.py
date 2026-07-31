"""Previous/next navigation between bills in the verification queue.

The detail page has always had a "Next" button, but each module computed it
differently and none of them was positional:

* Zoho took ``[0]`` of an unordered queryset — effectively an arbitrary row,
  so pressing Next twice could bounce between the same two bills.
* Tally took the oldest analysed bill, which meant Next returned the *same*
  bill every time until that one was processed.

Neither has a meaningful inverse, which is why there was no "Back". This
module replaces both with true positional navigation: bills are ordered the
way the list page shows them (newest first, see ``order_by('-created_at')``
in the list views), and a bill's neighbours in that order become its previous
and next.

Ties on ``created_at`` are broken by ``id`` so the sequence is stable — bills
uploaded in the same batch often share a timestamp to the second, and without
a tiebreak Next/Back could loop between them.
"""

from django.db.models import Q

# Bills still awaiting verification. Both modules use this exact string
# (Tally via ``BillStatus.ANALYSED``, Zoho as a literal).
QUEUE_STATUS = "Analysed"


def _queue(bill, status):
    """Bills of the same type and organisation that are still to be processed."""
    return (
        bill.__class__.objects.filter(organization=bill.organization, status=status)
        .exclude(id=bill.id)
    )


def get_adjacent_bill_ids(bill, status=QUEUE_STATUS):
    """Return ``(previous_id, next_id)`` as strings, or ``None`` at either end.

    Ordering is newest-first, matching the list view:

        previous  <-  [ newer bills ]  current  [ older bills ]  ->  next

    The current bill does not need to be in the queue itself. A verified bill
    still has neighbours, because position is decided by ``created_at`` rather
    than by membership of the filtered set.
    """
    created_at = getattr(bill, "created_at", None)
    if created_at is None:
        return None, None

    queue = _queue(bill, status)

    # "Next" moves down the newest-first list, i.e. towards older bills.
    next_id = (
        queue.filter(
            Q(created_at__lt=created_at)
            | Q(created_at=created_at, id__lt=bill.id)
        )
        .order_by("-created_at", "-id")
        .values_list("id", flat=True)
        .first()
    )

    # "Previous" moves back up towards newer bills.
    previous_id = (
        queue.filter(
            Q(created_at__gt=created_at)
            | Q(created_at=created_at, id__gt=bill.id)
        )
        .order_by("created_at", "id")
        .values_list("id", flat=True)
        .first()
    )

    return (
        str(previous_id) if previous_id else None,
        str(next_id) if next_id else None,
    )

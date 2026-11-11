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

Navigation walks every bill the list page shows, not only the ones still
awaiting verification. It used to filter on ``status="Analysed"``, which
meant an organisation with six purchase vouchers and one unverified bill
had both Next and Back greyed out on that bill — correct for a work
queue, but the operator is looking at a list of six and reads it as the
buttons being broken. Checking a bill against its neighbours is a normal
thing to want after it is verified. Callers that do want queue-only
movement can still pass ``status``.
"""

from django.db.models import Q

# Bills still awaiting verification. Both modules use this exact string
# (Tally via ``BillStatus.ANALYSED``, Zoho as a literal).
QUEUE_STATUS = "Analysed"


def _queue(bill, status=None):
    """The bills of the same type and organisation this one sits among.

    Trashed bills are excluded: a bill someone deleted must not turn up as
    the next thing to verify. Applied on capability (``alive()``) so this
    stays correct for any bill model, trashable or not.

    ``status`` narrows the set to one status when a caller asks for it.
    """
    manager = bill.__class__.objects
    base = manager.alive() if hasattr(manager, "alive") else manager.all()
    queryset = base.filter(organization=bill.organization)
    if status:
        queryset = queryset.filter(status=status)
    return queryset.exclude(id=bill.id)


def get_adjacent_bill_ids(bill, status=None):
    """Return ``(previous_id, next_id)`` as strings, or ``None`` at either end.

    Ordering is newest-first, matching the list view:

        previous  <-  [ newer bills ]  current  [ older bills ]  ->  next

    Every bill of this type in the organisation is a candidate, so a
    verified bill has neighbours just as an unverified one does. Pass
    ``status`` to walk only the bills with that status.
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

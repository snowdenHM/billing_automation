"""Who picks up a new ticket.

Advisory, not binding: any agent may reassign afterwards. The point is
that a ticket arrives with a name on it rather than sitting in a pool
nobody owns — and that the name is usually someone who has seen this
client's setup before, because the first thing a fresh agent does is ask
the client to explain it again.
"""
import logging

from django.db.models import Count, Q
from django.utils import timezone

logger = logging.getLogger(__name__)

OPEN_STATUSES = ("open", "in_progress")


def pick_agent(ticket):
    """The agent who should take *ticket*, or ``None``.

    Order: agents who have handled this organization before, then everyone
    else; within each, fewest open tickets first, oldest assignment next.
    Anybody at or over ``max_open_tickets`` is skipped — the round robin
    exists to spread load, so handing a full queue one more is the one
    outcome it must not produce.
    """
    from .models import SupportAgent, SupportTicket

    candidates = (
        SupportAgent.objects.filter(is_active=True, user__is_active=True, user__is_staff=True)
        .select_related("user")
        .annotate(
            open_tickets=Count(
                "user__assigned_support_tickets",
                filter=Q(
                    user__assigned_support_tickets__status__in=OPEN_STATUSES,
                    user__assigned_support_tickets__is_deleted=False,
                ),
                distinct=True,
            )
        )
    )

    available = [a for a in candidates if a.open_tickets < a.max_open_tickets]
    if not available:
        return None

    familiar = set()
    if ticket.organization_id:
        familiar = set(
            SupportTicket.objects.filter(
                organization_id=ticket.organization_id,
                assigned_to__isnull=False,
            )
            .exclude(pk=ticket.pk)
            .values_list("assigned_to_id", flat=True)
        )

    def rank(agent):
        # Sorting by the agent's id last keeps the order stable, so two
        # tickets created in the same second do not both land on whoever
        # the database felt like returning first.
        return (0 if agent.user_id in familiar else 1, agent.open_tickets, agent.user_id)

    return min(available, key=rank).user


def auto_assign(ticket):
    """Assign *ticket* if it is unassigned. Returns the agent or ``None``.

    No agent free means the ticket stays unassigned and shows up in the
    queue that way — better than an assignment nobody can honour.
    """
    if ticket.assigned_to_id:
        return ticket.assigned_to

    agent_user = pick_agent(ticket)
    if agent_user is None:
        logger.info("No support agent available for ticket %s.", ticket.pk)
        return None

    ticket.assigned_to = agent_user
    ticket.assigned_at = timezone.now()
    ticket.save(update_fields=["assigned_to", "assigned_at", "updated_at"])
    return agent_user

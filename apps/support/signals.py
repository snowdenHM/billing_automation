"""Ticket lifecycle hooks.

Both of these are signals rather than jobs on purpose. A queued job leaves
a window — however short — in which the ticket is shut and the door into
the client's account is still open. There must not be a window.
"""
import logging

from django.db import transaction
from django.utils import timezone
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import SupportAccessGrant, SupportAccessSession, SupportTicket

logger = logging.getLogger(__name__)

CLOSING_STATUSES = (SupportTicket.STATUS_RESOLVED, SupportTicket.STATUS_CLOSED)


@receiver(post_save, sender=SupportTicket, dispatch_uid="support_ticket_access_lifecycle")
def ticket_access_lifecycle(sender, instance, created, **kwargs):
    if created:
        # Deferred to after commit: the ticket-creation request has not
        # committed yet, and an assignment written inside that transaction
        # would vanish with it if the request then failed.
        transaction.on_commit(lambda: _assign_safely(instance.pk))
        return

    if instance.status in CLOSING_STATUSES:
        revoke_grants_for_ticket(instance)


def _assign_safely(ticket_pk):
    from .assignment import auto_assign

    ticket = SupportTicket.objects.filter(pk=ticket_pk).first()
    if ticket is None:
        return
    try:
        auto_assign(ticket)
    except Exception:  # pragma: no cover - assignment is advisory
        logger.exception("Auto-assignment failed for ticket %s.", ticket_pk)


def revoke_grants_for_ticket(ticket):
    """Kill every live grant on *ticket*, and the sessions on them.

    Access must end with the reason for it. The reason is the ticket.
    """
    live = SupportAccessGrant.objects.filter(
        ticket=ticket,
        status__in=(SupportAccessGrant.STATUS_PENDING, SupportAccessGrant.STATUS_ACTIVE),
    )
    now = timezone.now()
    count = 0
    for grant in live:
        grant.status = SupportAccessGrant.STATUS_CLOSED
        grant.revoked_at = grant.revoked_at or now
        grant.revoke_reason = f"Ticket {ticket.status}"
        grant.save(update_fields=["status", "revoked_at", "revoke_reason"])
        grant.sessions.filter(ended_at__isnull=True).update(
            ended_at=grant.revoked_at,
            end_reason=SupportAccessSession.END_TICKET_CLOSED,
        )
        count += 1
    if count:
        logger.info(
            "Ticket %s became %s — closed %s support access grant(s).",
            ticket.pk, ticket.status, count,
        )
    return count

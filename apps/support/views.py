from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from drf_spectacular.utils import extend_schema

from .emails import notify_recipients_new_ticket, notify_reply, notify_recipients_reply
from .models import SupportTicket, SupportTicketMessage
from .serializers import (
    SupportTicketCreateSerializer,
    SupportTicketMessageSerializer,
    SupportTicketSerializer,
)


def _current_organization(request):
    """Best-effort resolve the caller's active organization, mirroring the
    pattern used elsewhere (``request.user.memberships``) without hard
    failing when one isn't selected — the ticket is still creatable."""
    membership = getattr(request.user, "memberships", None)
    if membership is None:
        return None
    active = membership.filter(is_active=True).select_related("organization").first()
    return active.organization if active else None


@extend_schema(
    request=SupportTicketCreateSerializer,
    responses=SupportTicketSerializer,
    tags=["Support"],
    methods=["POST"],
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def support_ticket_create_view(request):
    """Create a support ticket for the authenticated user and notify the
    dev-team distribution list (``SupportTicketRecipient`` rows with
    ``receive_new=True``)."""
    serializer = SupportTicketCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    ticket = serializer.save(
        user=request.user,
        organization=_current_organization(request),
    )

    notify_recipients_new_ticket(ticket)

    return Response(
        SupportTicketSerializer(ticket, context={"request": request}).data,
        status=status.HTTP_201_CREATED,
    )


@extend_schema(
    responses=SupportTicketSerializer(many=True),
    tags=["Support"],
    methods=["GET"],
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def support_ticket_mine_view(request):
    """List the current user's own tickets."""
    tickets = SupportTicket.objects.filter(
        user=request.user, is_deleted=False
    ).order_by("-created_at")
    serializer = SupportTicketSerializer(tickets, many=True, context={"request": request})
    return Response({"data": serializer.data})


@extend_schema(
    request=SupportTicketMessageSerializer,
    responses=SupportTicketSerializer,
    tags=["Support"],
    methods=["POST"],
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def support_ticket_reply_view(request, pk):
    """Append a message to a ticket's thread — the owning user or an admin
    (staff) may reply. Notifies the other party by email:
      - user replies  -> notify dev-team recipients subscribed to replies
      - admin replies -> notify the ticket's submitter (if any)
    """
    ticket = get_object_or_404(SupportTicket, pk=pk, is_deleted=False)

    is_owner = ticket.user_id == request.user.id
    is_staff = bool(request.user.is_staff)
    if not (is_owner or is_staff):
        return Response(
            {"detail": "You do not have permission to reply to this ticket."},
            status=status.HTTP_403_FORBIDDEN,
        )

    body = (request.data or {}).get("body", "").strip()
    if not body:
        return Response({"detail": "body is required."}, status=status.HTTP_400_BAD_REQUEST)

    is_internal = bool((request.data or {}).get("is_internal", False)) and is_staff

    message = SupportTicketMessage.objects.create(
        ticket=ticket,
        author_user=request.user,
        author_email=request.user.email,
        body=body,
        is_internal=is_internal,
    )

    if not is_internal:
        if is_staff and not is_owner:
            # Admin replying to a user's ticket — notify the submitter.
            submitter_email = ticket.user.email if ticket.user_id else None
            notify_reply(ticket, message, submitter_email)
        else:
            # Submitter replying — notify the dev-team distribution list.
            notify_recipients_reply(ticket, message)

    return Response(
        SupportTicketSerializer(ticket, context={"request": request}).data,
        status=status.HTTP_201_CREATED,
    )

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from drf_spectacular.utils import extend_schema

from apps.common.authentication import OptionalJWTAuthentication
from apps.common.recaptcha import get_client_ip, verify_token
from apps.common.utils import get_rate_limit_ip, is_rate_limited

from .emails import (
    notify_recipients_new_ticket,
    notify_recipients_reply,
    notify_reply,
    notify_support_chat,
)
from .models import SupportTicket, SupportTicketMessage
from .serializers import (
    SupportChatSerializer,
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
            # Chat tickets from website visitors have no user — reply to
            # the email they left in the chat (Correction 51).
            submitter_email = ticket.user.email if ticket.user_id else (ticket.contact_email or None)
            notify_reply(ticket, message, submitter_email)
        else:
            # Submitter replying — notify the dev-team distribution list.
            notify_recipients_reply(ticket, message)

    return Response(
        SupportTicketSerializer(ticket, context={"request": request}).data,
        status=status.HTTP_201_CREATED,
    )


@extend_schema(
    request=SupportChatSerializer,
    tags=["Support"],
    methods=["POST"],
)
@api_view(["POST"])
@authentication_classes([OptionalJWTAuthentication])
@permission_classes([AllowAny])
def support_chat_view(request):
    """Client Correction 51 — floating support chat (in the app and on the
    public website). Saves the message as a ticket (source=chat) so it shows
    up in admin, and emails it to the support inbox.

    Anonymous visitors must pass reCAPTCHA (when configured); everyone is
    rate-limited per IP.
    """
    too_many = Response(
        {"message": "Too many messages. Please wait a few minutes and try again."},
        status=status.HTTP_429_TOO_MANY_REQUESTS,
    )
    if is_rate_limited("support-chat-ip", get_rate_limit_ip(request), limit=10, window_seconds=600):
        return too_many

    serializer = SupportChatSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    user = request.user if request.user and request.user.is_authenticated else None
    if user is None:
        ok, captcha_message = verify_token(data.get("recaptcha_token", ""), get_client_ip(request))
        if not ok:
            return Response(
                {"recaptcha_token": [captcha_message]},
                status=status.HTTP_400_BAD_REQUEST,
            )

    # Counted only for messages that passed validation + captcha, so junk
    # requests can't use up someone else's (or everyone's) quota.
    if is_rate_limited("support-chat-email", data["email"], limit=5, window_seconds=600):
        return too_many
    if user is None and is_rate_limited("support-chat-all", "global", limit=300, window_seconds=3600):
        return too_many

    message = data["message"]
    first_line = message.splitlines()[0] if message else ""
    subject = f"Chat: {first_line}"[:200]

    ticket = SupportTicket.objects.create(
        user=user,
        organization=_current_organization(request) if user else None,
        subject=subject,
        message=message,
        category=SupportTicket.CATEGORY_QUESTION,
        source=SupportTicket.SOURCE_CHAT,
        contact_name=data["name"],
        contact_email=data["email"].lower(),
        page_url=(data.get("page_url") or "")[:500],
        browser=(request.META.get("HTTP_USER_AGENT") or "")[:200],
    )

    notify_support_chat(ticket)

    return Response(
        {
            "success": True,
            "message": "Thanks! Our support team will get back to you shortly.",
            "id": str(ticket.id),
        },
        status=status.HTTP_201_CREATED,
    )

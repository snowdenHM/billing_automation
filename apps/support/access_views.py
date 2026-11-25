"""Endpoints for consent-gated support access, and the support desk.

Two rules shape the client-side half of this file:

* Only a client admin mints or revokes. No support endpoint creates a
  grant, which is what makes "access must be asked for" true rather than
  aspirational.
* The admin check here does **not** fall through for ``is_staff``, unlike
  ``apps.common.permissions.assert_org_access``. That shortcut is right
  for the rest of the product — staff support the product — and exactly
  wrong here: it would let a support agent mint their own consent.
"""
import logging

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.organizations.models import Organization, OrgMembership

from . import access_tokens
from .access_serializers import (
    AccessGrantMintSerializer,
    AccessRedeemSerializer,
    AccessRevokeSerializer,
    DeskAssignSerializer,
    DeskReplySerializer,
    DeskTicketSerializer,
    DeskTicketUpdateSerializer,
    SupportAccessEventSerializer,
    SupportAccessGrantSerializer,
    SupportAccessSessionSerializer,
)
from .emails import notify_reply
from .guards import IsSupportAgent, NotImpersonated
from .impersonation import ImpersonationToken, session_from_request
from .models import (
    SupportAccessGrant,
    SupportAccessSession,
    SupportTicket,
    SupportTicketMessage,
)
from .serializers import SupportTicketSerializer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Client side
# ---------------------------------------------------------------------------

def _require_org_admin(request, organization):
    """The caller must hold an active ADMIN membership on *organization*.

    Membership, and nothing else. Superusers and staff are not admins of
    a client's organization for the purpose of consent — if they were,
    the support team could grant itself access and the whole mechanism
    would be decoration.
    """
    user = request.user
    if not user or not user.is_authenticated:
        raise PermissionDenied("Authentication required.")
    is_admin = OrgMembership.objects.filter(
        organization=organization,
        user=user,
        role=OrgMembership.ADMIN,
        is_active=True,
    ).exists()
    if not is_admin:
        raise PermissionDenied("You must be an organization admin to manage support access.")
    if hasattr(user, "support_agent"):
        # A support agent who is also a member of a client organization
        # (a demo account, a colleague's personal org) must still not be
        # the one who says yes. Consent has to come from the other side.
        logger.warning(
            "Support agent %s attempted to mint/revoke access on org %s — refused.",
            user.pk, organization.pk,
        )
        raise PermissionDenied("A support agent cannot grant or revoke their own access.")


def _require_org_member(request, organization):
    user = request.user
    if not user or not user.is_authenticated:
        raise PermissionDenied("Authentication required.")
    if not OrgMembership.objects.filter(
        organization=organization, user=user, is_active=True
    ).exists():
        raise PermissionDenied("You don't have permission to access this organization.")


@extend_schema(
    request=AccessGrantMintSerializer,
    tags=["Support access"],
    methods=["POST"],
)
@api_view(["POST"])
@permission_classes([IsAuthenticated, NotImpersonated])
def mint_access_grant_view(request, ticket_id):
    """Mint a grant on a ticket. **Returns the full code once.**

    The response body is the only place the plaintext ever exists after
    this function returns. It is not logged, not stored and not
    retrievable — a client admin who loses it mints another.
    """
    ticket = get_object_or_404(SupportTicket, pk=ticket_id, is_deleted=False)
    if not ticket.organization_id:
        # A chat ticket from a visitor has no organization, so there is no
        # account for a grant to open.
        raise PermissionDenied("This ticket is not attached to an organization.")
    _require_org_admin(request, ticket.organization)

    if ticket.status in (SupportTicket.STATUS_RESOLVED, SupportTicket.STATUS_CLOSED):
        return Response(
            {"detail": "This ticket is closed; reopen it before granting access."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    serializer = AccessGrantMintSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    ttl_minutes = min(
        serializer.validated_data["ttl_minutes"], SupportAccessGrant.MAX_TTL_MINUTES
    )

    code, code_label, token_hash = access_tokens.mint_code()
    grant = SupportAccessGrant.objects.create(
        ticket=ticket,
        organization=ticket.organization,
        created_by=request.user,
        code_label=code_label,
        token_hash=token_hash,
        scope=serializer.validated_data["scope"],
        expires_at=timezone.now() + timezone.timedelta(minutes=ttl_minutes),
        status=SupportAccessGrant.STATUS_PENDING,
    )

    body = SupportAccessGrantSerializer(grant).data
    body["code"] = code
    return Response(body, status=status.HTTP_201_CREATED)


@extend_schema(
    responses=SupportAccessGrantSerializer(many=True),
    tags=["Support access"],
    methods=["GET"],
)
@api_view(["GET"])
@permission_classes([IsAuthenticated, NotImpersonated])
def organization_access_grants_view(request, org_id):
    """Every grant on this organization, with its sessions."""
    organization = get_object_or_404(Organization, pk=org_id)
    _require_org_admin(request, organization)

    grants = (
        SupportAccessGrant.objects.filter(organization=organization)
        .select_related("ticket", "redeemed_by")
        .prefetch_related("sessions__agent__support_agent")
    )
    return Response(
        {"data": SupportAccessGrantSerializer(grants, many=True).data}
    )


@extend_schema(
    request=AccessRevokeSerializer,
    responses=SupportAccessGrantSerializer,
    tags=["Support access"],
    methods=["POST"],
)
@api_view(["POST"])
@permission_classes([IsAuthenticated, NotImpersonated])
def revoke_access_grant_view(request, grant_id):
    """Revoke now. Live sessions die with it, on this request."""
    grant = get_object_or_404(
        SupportAccessGrant.objects.select_related("organization"), pk=grant_id
    )
    # Any active member, not only an ADMIN — deliberately asymmetric with
    # minting.
    #
    # Minting hands out power and is an admin decision. Ending access is
    # withdrawing it, and the moment it is needed is the moment something
    # looks wrong on someone's screen. If only the admin who happened to
    # be on the phone can stop it, the control fails exactly when it
    # matters: they have put the phone down, or gone home. The banner
    # offers "End access" to everyone who can see it, so the endpoint
    # behind that button has to answer to everyone who can see it too.
    #
    # The blast radius of an unnecessary revoke is a support agent having
    # to ask for a new code. The blast radius of a revoke that will not
    # fire is the thing this whole feature exists to prevent.
    _require_org_member(request, grant.organization)

    serializer = AccessRevokeSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    grant.revoke(
        by=request.user,
        reason=serializer.validated_data.get("reason") or "Revoked by client",
    )
    grant.refresh_from_db()
    return Response(SupportAccessGrantSerializer(grant).data)


@extend_schema(tags=["Support access"], methods=["GET"])
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def organization_access_status_view(request, org_id):
    """Is support inside this account right now? Drives the banner.

    Open to any member, not only the admin who minted: the person looking
    at their own invoices is the one who needs to know.
    """
    organization = get_object_or_404(Organization, pk=org_id)
    _require_org_member(request, organization)

    live = (
        SupportAccessSession.objects.filter(
            grant__organization=organization, ended_at__isnull=True
        )
        .select_related("grant", "agent__support_agent")
        .order_by("-started_at")
    )
    # Filtered in Python because "live" is a question about the clock as
    # well as the columns, and the clock is not in the query.
    live = [s for s in live if s.grant.is_usable()]
    return Response(
        {
            "active": bool(live),
            "sessions": SupportAccessSessionSerializer(live, many=True).data,
        }
    )


# ---------------------------------------------------------------------------
# Support desk
# ---------------------------------------------------------------------------

@extend_schema(responses=DeskTicketSerializer(many=True), tags=["Support desk"], methods=["GET"])
@api_view(["GET"])
@permission_classes([IsAuthenticated, IsSupportAgent])
def desk_ticket_list_view(request):
    """The queue. ``?status=`` and ``?assignee=me|unassigned|<id>``."""
    tickets = (
        SupportTicket.objects.filter(is_deleted=False)
        .select_related("organization", "user", "assigned_to__support_agent")
        .prefetch_related("access_grants__sessions")
    )
    status_filter = request.query_params.get("status")
    if status_filter:
        tickets = tickets.filter(status=status_filter)

    assignee = request.query_params.get("assignee")
    if assignee == "me":
        tickets = tickets.filter(assigned_to=request.user)
    elif assignee == "unassigned":
        tickets = tickets.filter(assigned_to__isnull=True)
    elif assignee:
        tickets = tickets.filter(assigned_to_id=assignee)

    return Response({"data": DeskTicketSerializer(tickets, many=True).data})


def _agent_display_name(user):
    """What a support agent is called on screen.

    Their display_name if they have a SupportAgent row, else their
    email. Never a bare id — an operator cannot act on "Agent 27".
    """
    if user is None:
        return None
    agent_row = getattr(user, "support_agent", None)
    return agent_row.display_name if agent_row else user.email

@extend_schema(responses=SupportTicketSerializer, tags=["Support desk"], methods=["GET", "PATCH"])
@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated, IsSupportAgent])
def desk_ticket_detail_view(request, ticket_id):
    """One ticket and its thread; PATCH changes status or priority."""
    ticket = get_object_or_404(
        SupportTicket.objects.select_related("organization", "user"),
        pk=ticket_id,
        is_deleted=False,
    )

    if request.method == "PATCH":
        serializer = DeskTicketUpdateSerializer(ticket, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        if serializer.validated_data.get("status") == SupportTicket.STATUS_RESOLVED:
            ticket.resolved_at = timezone.now()
        # Saved through the model so the lifecycle signal fires — the
        # grants on a ticket the desk just resolved must die here, not on
        # the next sweep.
        serializer.save()

    payload = SupportTicketSerializer(ticket, context={"request": request}).data
    payload["assigned_to"] = ticket.assigned_to_id
    # The queue sends the agent's display_name; this endpoint sent only
    # the pk, so the detail page fell back to printing "Agent 27". Same
    # rule both places: the name the agent chose, their email if they
    # never set one.
    payload["assigned_to_name"] = _agent_display_name(ticket.assigned_to)
    payload["grants"] = SupportAccessGrantSerializer(
        ticket.access_grants.all(), many=True
    ).data
    return Response(payload)


@extend_schema(request=DeskReplySerializer, tags=["Support desk"], methods=["POST"])
@api_view(["POST"])
@permission_classes([IsAuthenticated, IsSupportAgent])
def desk_ticket_reply_view(request, ticket_id):
    """Public reply, or an internal note the client never sees."""
    ticket = get_object_or_404(SupportTicket, pk=ticket_id, is_deleted=False)
    serializer = DeskReplySerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    message = SupportTicketMessage.objects.create(
        ticket=ticket,
        author_user=request.user,
        author_email=request.user.email,
        body=serializer.validated_data["body"],
        is_internal=serializer.validated_data["is_internal"],
    )
    if not message.is_internal:
        submitter = ticket.user.email if ticket.user_id else (ticket.contact_email or None)
        notify_reply(ticket, message, submitter)

    return Response(
        SupportTicketSerializer(ticket, context={"request": request}).data,
        status=status.HTTP_201_CREATED,
    )


@extend_schema(request=DeskAssignSerializer, tags=["Support desk"], methods=["POST"])
@api_view(["POST"])
@permission_classes([IsAuthenticated, IsSupportAgent])
def desk_ticket_assign_view(request, ticket_id):
    """Assign or reassign. Assignment is advisory — any agent may move it.

    Capacity is not enforced here: ``max_open_tickets`` steers the
    automatic round robin, and a human who decides a ticket belongs with
    a particular agent knows something the counter does not.
    """
    from django.contrib.auth import get_user_model

    ticket = get_object_or_404(SupportTicket, pk=ticket_id, is_deleted=False)
    serializer = DeskAssignSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    if "agent_id" not in serializer.validated_data:
        target = request.user
    elif serializer.validated_data["agent_id"] is None:
        target = None
    else:
        User = get_user_model()
        target = User.objects.filter(
            pk=serializer.validated_data["agent_id"],
            is_staff=True,
            support_agent__isnull=False,
        ).first()
        if target is None:
            return Response(
                {"detail": "That user is not a support agent."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    ticket.assigned_to = target
    ticket.assigned_at = timezone.now() if target else None
    ticket.save(update_fields=["assigned_to", "assigned_at", "updated_at"])
    return Response(DeskTicketSerializer(ticket).data)


@extend_schema(request=AccessRedeemSerializer, tags=["Support desk"], methods=["POST"])
@api_view(["POST"])
@permission_classes([IsAuthenticated, IsSupportAgent])
def desk_access_redeem_view(request):
    """Trade the spoken code for an impersonation token."""
    serializer = AccessRedeemSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    try:
        access_tokens.check_lockout(request.user.pk)
    except access_tokens.RedemptionLocked as locked:
        return Response(
            {
                "detail": "Too many failed attempts. Try again shortly.",
                "retry_after_seconds": locked.retry_after_seconds,
            },
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    grant = access_tokens.find_grant_for_code(serializer.validated_data["code"])
    if grant is None:
        # One message for "no such code", "expired" and "revoked" alike:
        # distinguishing them tells a guesser which half of the search
        # space to keep.
        access_tokens.record_failure(request.user.pk)
        return Response(
            {"detail": "That code is not valid."}, status=status.HTTP_400_BAD_REQUEST
        )

    if grant.redeemed_by_id and grant.redeemed_by_id != request.user.pk:
        # A grant redeems once. A second agent asking for the same consent
        # is a different person than the client said yes to.
        access_tokens.record_failure(request.user.pk)
        logger.warning(
            "Agent %s tried to redeem grant %s already held by agent %s — refused.",
            request.user.pk, grant.pk, grant.redeemed_by_id,
        )
        return Response(
            {"detail": "That code has already been redeemed by another agent."},
            status=status.HTTP_403_FORBIDDEN,
        )

    client_user = _impersonation_subject(grant)
    if client_user is None:
        return Response(
            {"detail": "This organization has no member to act as."},
            status=status.HTTP_409_CONFLICT,
        )

    with transaction.atomic():
        session = grant.sessions.filter(agent=request.user, ended_at__isnull=True).first()
        if session is None:
            session = SupportAccessSession.objects.create(
                grant=grant,
                agent=request.user,
                ip=_client_ip(request),
                user_agent=(request.META.get("HTTP_USER_AGENT") or "")[:300],
            )
        if not grant.redeemed_at:
            grant.redeemed_at = timezone.now()
            grant.redeemed_by = request.user
        grant.status = SupportAccessGrant.STATUS_ACTIVE
        grant.save(update_fields=["status", "redeemed_at", "redeemed_by"])

    session.grant = grant
    token = ImpersonationToken.for_session(session, client_user=client_user)
    access_tokens.clear_failures(request.user.pk)
    logger.info(
        "Agent %s opened support session %s on org %s (grant %s, scope %s).",
        request.user.pk, session.pk, grant.organization_id, grant.pk, grant.scope,
    )

    return Response(
        {
            "token": str(token),
            "session": SupportAccessSessionSerializer(session).data,
            "grant": SupportAccessGrantSerializer(grant).data,
            "organization": {
                "id": str(grant.organization_id),
                "name": grant.organization.name,
            },
            "acting_as": {"id": client_user.pk, "email": client_user.email},
            "scope": grant.scope,
            "expires_at": grant.expires_at,
        }
    )


def _impersonation_subject(grant):
    """The client user the session runs as.

    The ticket's author when they are still in the organization, else the
    admin who minted the grant — somebody who is demonstrably inside the
    account, so the session inherits their rights and no more.
    """
    from django.contrib.auth import get_user_model

    User = get_user_model()
    for candidate_id in (grant.ticket.user_id, grant.created_by_id):
        if not candidate_id:
            continue
        user = User.objects.filter(
            pk=candidate_id,
            is_active=True,
            memberships__organization_id=grant.organization_id,
            memberships__is_active=True,
        ).first()
        if user:
            return user
    return (
        User.objects.filter(
            is_active=True,
            memberships__organization_id=grant.organization_id,
            memberships__is_active=True,
            memberships__role=OrgMembership.ADMIN,
        )
        .order_by("pk")
        .first()
    )


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    return (request.META.get("REMOTE_ADDR") or "")[:45]


@extend_schema(tags=["Support desk"], methods=["POST"])
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def desk_access_end_view(request):
    """End the caller's own session.

    Callable two ways, because an agent hangs up from either side: with
    their own desk token, or from inside the impersonated session (the one
    unsafe method a ``read_only`` grant keeps).
    """
    session = session_from_request(request)
    if session is None:
        if not (request.user.is_staff and hasattr(request.user, "support_agent")):
            raise PermissionDenied("Only a support agent may end a support session.")
        session = (
            SupportAccessSession.objects.filter(agent=request.user, ended_at__isnull=True)
            .select_related("grant")
            .order_by("-started_at")
            .first()
        )
    if session is None:
        return Response({"detail": "No live session."}, status=status.HTTP_404_NOT_FOUND)

    session.end(reason=SupportAccessSession.END_SIGNED_OUT)
    grant = session.grant
    if not grant.sessions.filter(ended_at__isnull=True).exists():
        grant.status = SupportAccessGrant.STATUS_CLOSED
        grant.save(update_fields=["status"])
    return Response(SupportAccessSessionSerializer(session).data)


@extend_schema(
    responses=SupportAccessSessionSerializer(many=True),
    tags=["Support desk"],
    methods=["GET"],
)
@api_view(["GET"])
@permission_classes([IsAuthenticated, IsSupportAgent])
def desk_access_sessions_view(request):
    """The caller's own access history, newest first, with the trail."""
    sessions = (
        SupportAccessSession.objects.filter(agent=request.user)
        .select_related("grant", "grant__organization", "agent__support_agent")
        .prefetch_related("events")
    )
    data = []
    for session in sessions:
        row = SupportAccessSessionSerializer(session).data
        row["organization_name"] = session.grant.organization.name
        row["events"] = SupportAccessEventSerializer(
            session.events.all()[:100], many=True
        ).data
        data.append(row)
    return Response({"data": data})

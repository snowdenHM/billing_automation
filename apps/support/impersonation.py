"""The impersonation token, and the authentication that accepts it.

Redeeming a grant returns a JWT that is deliberately *not* a normal user
token: it carries ``act`` (the agent) alongside ``sub`` (the client), so
the request runs as the client and every log line still names the agent.
A token without ``act`` can never come out of this flow, and a token with
``act`` is refused by the endpoints under "forbidden while impersonating".

Signing is simplejwt's, not ours: the project already has one signing key,
one algorithm and one place they are configured, and a second hand-rolled
signer is a second thing to get wrong.
"""
import logging

import jwt as pyjwt
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import AuthenticationFailed, InvalidToken, TokenError
from rest_framework_simplejwt.tokens import Token

from apps.organizations.models import OrgMembership

logger = logging.getLogger(__name__)

TOKEN_TYPE = "impersonation"


class ImpersonationToken(Token):
    """A support-session token. ``token_type`` keeps it out of every code
    path that expects an access token: simplejwt's ``AccessToken`` refuses
    it outright, so it cannot be presented as an ordinary login."""

    token_type = TOKEN_TYPE
    # Never used as written: ``for_session`` always sets ``exp`` from the
    # grant. Declared because simplejwt requires a lifetime on the class.
    lifetime = timezone.timedelta(minutes=60)

    @classmethod
    def for_session(cls, session, *, client_user):
        """Mint the token for *session*.

        ``exp`` is the grant's own expiry, never later. A token that
        outlived its grant would be a standing capability again, which is
        the whole thing this design removes.
        """
        grant = session.grant
        now = timezone.now()
        token = cls()
        token.set_exp(from_time=now, lifetime=max(grant.expires_at - now, timezone.timedelta(0)))
        token["sub"] = str(client_user.id)
        token["act"] = str(session.agent_id)
        token["grant"] = str(grant.id)
        token["session"] = str(session.id)
        token["scope"] = grant.scope
        return token


def _peek_token_type(raw_token):
    """The ``token_type`` claim, read without verifying anything.

    Only used to decide whether this authentication class is the one that
    should speak. Everything the claim is then trusted for is re-read from
    the verified payload below.
    """
    try:
        payload = pyjwt.decode(raw_token, options={"verify_signature": False})
    except Exception:
        return None
    return payload.get("token_type")


class SupportImpersonationAuthentication(JWTAuthentication):
    """Accept an impersonation token: ``request.user`` becomes the client,
    ``request.support_session`` the session it is running inside.

    Fails closed. Expiry is judged against the clock and revocation
    against the grant's own columns, so a grant that has passed its TTL —
    or that was revoked a second ago, or whose ticket has just been shut —
    stops authenticating immediately, whether or not a sweeper has
    relabelled ``status`` yet.
    """

    def authenticate(self, request):
        header = self.get_header(request)
        if header is None:
            return None
        raw_token = self.get_raw_token(header)
        if raw_token is None:
            return None
        if _peek_token_type(raw_token) != TOKEN_TYPE:
            # Somebody else's token. Returning None lets the ordinary
            # JWTAuthentication behind us handle it.
            return None

        try:
            token = ImpersonationToken(raw_token)
        except TokenError as exc:
            raise InvalidToken(str(exc))

        session = self._resolve_session(token)
        client_user = self._resolve_client_user(token, session)

        # Stamped on the underlying HttpRequest as well: the audit
        # middleware runs outside DRF and only ever sees that object.
        underlying = getattr(request, "_request", None)
        for target in filter(None, (request, underlying)):
            target.support_session = session
            target.support_grant = session.grant
            target.support_scope = session.grant.scope
            target.support_agent_id = session.agent_id

        return (client_user, token)

    def _resolve_session(self, token):
        from .models import SupportAccessSession

        session_id = token.get("session")
        grant_id = token.get("grant")
        agent_id = token.get("act")
        if not (session_id and grant_id and agent_id):
            raise AuthenticationFailed("Impersonation token is incomplete.")

        session = (
            SupportAccessSession.objects.filter(pk=session_id)
            .select_related("grant", "agent")
            .first()
        )
        if session is None:
            raise AuthenticationFailed("Support session not found.")
        if str(session.grant_id) != str(grant_id) or str(session.agent_id) != str(agent_id):
            # The token's own claims disagree with the row. Either the
            # token was tampered with or the session was rebound; neither
            # is a request we should carry.
            raise AuthenticationFailed("Impersonation token does not match its session.")
        if session.ended_at is not None:
            raise AuthenticationFailed("Support session has ended.")

        grant = session.grant
        if token.get("scope") != grant.scope:
            # A token may never claim a wider scope than the grant it
            # came from — read_only cannot be talked up to full.
            raise AuthenticationFailed("Impersonation scope no longer matches the grant.")

        if not grant.is_usable():
            # Fail closed, then tidy. The refusal does not wait on the
            # bookkeeping.
            self._close_out(session, grant)
            raise AuthenticationFailed("Support access grant is no longer valid.")

        if not self._agent_still_valid(session.agent):
            raise AuthenticationFailed("Support agent is no longer authorised.")

        return session

    @staticmethod
    def _agent_still_valid(agent):
        """Both halves, every request. ``is_staff`` alone is flipped by
        admin flows that have nothing to do with the support desk."""
        if agent is None or not agent.is_active or not agent.is_staff:
            return False
        return hasattr(agent, "support_agent")

    @staticmethod
    def _close_out(session, grant):
        from .models import SupportAccessGrant, SupportAccessSession

        reason = (
            SupportAccessSession.END_EXPIRED
            if grant.is_expired
            else SupportAccessSession.END_REVOKED
        )
        session.end(reason=reason)
        if grant.is_expired and grant.status not in (
            SupportAccessGrant.STATUS_REVOKED,
            SupportAccessGrant.STATUS_CLOSED,
            SupportAccessGrant.STATUS_EXPIRED,
        ):
            grant.status = SupportAccessGrant.STATUS_EXPIRED
            grant.save(update_fields=["status"])

    @staticmethod
    def _resolve_client_user(token, session):
        """The client the request runs as — and a check that they are
        still in the organization the grant opened. Consent is bound to
        one organization; a user who has left it is not inside it."""
        User = get_user_model()
        client_user = User.objects.filter(pk=token.get("sub"), is_active=True).first()
        if client_user is None:
            raise AuthenticationFailed("Impersonated user not found.")
        in_org = OrgMembership.objects.filter(
            user=client_user,
            organization_id=session.grant.organization_id,
            is_active=True,
        ).exists()
        if not in_org:
            raise AuthenticationFailed("Impersonated user is not a member of this organization.")
        return client_user


def session_from_request(request):
    """The live support session behind *request*, or ``None``.

    Used by the middleware, which runs before and after DRF and therefore
    cannot rely on the authentication class having stamped the request
    yet. Reads the stamp when it is there, and otherwise re-reads the
    token's claims — verified, but without the liveness checks above,
    because the middleware's job is to classify the request, not to admit
    it. Admission stays with the authentication class.
    """
    stamped = getattr(request, "support_session", None)
    if stamped is not None:
        return stamped

    header = request.META.get("HTTP_AUTHORIZATION", "")
    parts = header.split()
    if len(parts) != 2 or parts[0] != "Bearer":
        return None
    raw_token = parts[1]
    if _peek_token_type(raw_token) != TOKEN_TYPE:
        return None
    try:
        token = ImpersonationToken(raw_token)
    except TokenError:
        return None

    from .models import SupportAccessSession

    return (
        SupportAccessSession.objects.filter(pk=token.get("session"))
        .select_related("grant")
        .first()
    )

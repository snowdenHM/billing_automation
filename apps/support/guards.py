"""What an impersonated request may not do, and the record that it tried.

Two layers on purpose. The middleware is the one that actually holds:
it sees every path the project answers, including endpoints written after
this file and endpoints that forgot to name a permission class. The
permission classes are declared on the access endpoints as well, so a
future refactor that drops the middleware from ``MIDDLEWARE`` does not
silently open the mint endpoint to the people it exists to fence out.
"""
import logging
import re

from django.http import JsonResponse
from rest_framework import status
from rest_framework.permissions import SAFE_METHODS, BasePermission

from .impersonation import session_from_request

logger = logging.getLogger(__name__)

IMPERSONATION_FORBIDDEN = "IMPERSONATION_FORBIDDEN"

_UUID = r"[0-9a-fA-F-]{36}"

# Matched against the path *below* /api/v1/. These are the actions where
# "the client did it" and "support did it" must never be confused — so
# support cannot do them at all, at any scope.
#
# ``methods`` of None means the path is closed to every method, including
# GET: listing a client's grants from inside their own account would let
# an agent read the consent trail they are the subject of.
FORBIDDEN_RULES = [
    # Minting, listing or revoking access grants — consent cannot be
    # laundered through the session it granted.
    (re.compile(rf"^support/tickets/{_UUID}/access-grants/$"), None),
    (re.compile(rf"^support/access-grants/{_UUID}/revoke/$"), None),
    (re.compile(rf"^organizations/{_UUID}/access-grants/$"), None),
    # Passwords, email, 2FA. ``me/`` is readable; writing it changes the
    # email address, which is an account takeover by another name.
    (re.compile(r"^auth/password/"), None),
    (re.compile(r"^auth/verify-email/"), None),
    (re.compile(r"^auth/2fa/"), None),
    (re.compile(r"^me/$"), ["POST", "PUT", "PATCH", "DELETE"]),
    # Membership: inviting, removing, re-roling.
    (re.compile(rf"^org/{_UUID}/members/"), ["POST", "PUT", "PATCH", "DELETE"]),
    (re.compile(r"^memberships/"), ["POST", "PUT", "PATCH", "DELETE"]),
    # API keys — issuing one would turn a 60-minute grant into a
    # credential that outlives it.
    (re.compile(rf"^org/{_UUID}/api-keys/"), None),
    # Deleting the organization.
    (re.compile(rf"^org/{_UUID}/$"), ["DELETE"]),
    (re.compile(rf"^org/{_UUID}/update/$"), ["POST", "PUT", "PATCH", "DELETE"]),
    # Billing and subscriptions.
    (re.compile(rf"^organizations/{_UUID}/subscription/"), None),
    (re.compile(r"^subscriptions/"), ["POST", "PUT", "PATCH", "DELETE"]),
    (re.compile(r"^plans/"), ["POST", "PUT", "PATCH", "DELETE"]),
]

# The one unsafe method a read_only session keeps: ending its own session.
# Without it a read_only agent could not hang up.
SESSION_END_PATH = re.compile(r"^support-desk/access/end/$")

API_PREFIX = "/api/v1/"


def relative_path(path):
    """The request path below ``/api/v1/``, or "" when it is elsewhere."""
    if not path.startswith(API_PREFIX):
        return ""
    return path[len(API_PREFIX):]


def forbidden_while_impersonating(path, method):
    rel = relative_path(path)
    if not rel:
        return False
    for pattern, methods in FORBIDDEN_RULES:
        if pattern.match(rel) and (methods is None or method.upper() in methods):
            return True
    return False


def _denied(detail):
    return JsonResponse(
        {"code": IMPERSONATION_FORBIDDEN, "detail": detail},
        status=status.HTTP_403_FORBIDDEN,
    )


class SupportImpersonationMiddleware:
    """Refuse what support may not do, and write down what it did.

    One ``SupportAccessEvent`` per impersonated request — including the
    refused ones, because an attempt on a forbidden endpoint is the single
    most interesting line the trail can carry.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        session = session_from_request(request)
        if session is None:
            return self.get_response(request)

        method = request.method.upper()
        rel = relative_path(request.path)

        response = self._refuse(request, session, method, rel)
        if response is None:
            response = self.get_response(request)

        self._record(session, method, request.path, response.status_code)
        return response

    def _refuse(self, request, session, method, rel):
        if forbidden_while_impersonating(request.path, method):
            logger.warning(
                "Agent %s attempted %s %s while impersonating (grant %s) — refused.",
                session.agent_id, method, request.path, session.grant_id,
            )
            return _denied(
                "This action cannot be performed from a support access session."
            )

        read_only = session.grant.scope == session.grant.SCOPE_READ_ONLY
        if read_only and method not in SAFE_METHODS and not SESSION_END_PATH.match(rel):
            logger.warning(
                "Agent %s attempted %s %s on a read_only grant %s — refused.",
                session.agent_id, method, request.path, session.grant_id,
            )
            return _denied("This support session is read-only.")
        return None

    @staticmethod
    def _record(session, method, path, status_code):
        from .models import SupportAccessEvent

        try:
            SupportAccessEvent.objects.create(
                session=session,
                method=method[:10],
                path=path[:500],
                status_code=status_code,
            )
        except Exception:  # pragma: no cover - audit write must not 500 the request
            logger.exception(
                "Failed to record support access event for session %s.", session.pk
            )


class NotImpersonated(BasePermission):
    """Declared on the endpoints that must never be reachable from a
    support session, so the rule survives the middleware being moved."""

    def has_permission(self, request, view):
        if session_from_request(request) is None:
            return True
        from rest_framework.exceptions import PermissionDenied

        raise PermissionDenied(
            {
                "code": IMPERSONATION_FORBIDDEN,
                "detail": "This action cannot be performed from a support access session.",
            }
        )


class ImpersonationScopeAllows(BasePermission):
    """Refuse unsafe methods on a ``read_only`` session.

    A duplicate of the middleware's check, kept for views that may one
    day be served outside the ``/api/v1/`` tree the middleware matches on.
    """

    def has_permission(self, request, view):
        session = session_from_request(request)
        if session is None or request.method in SAFE_METHODS:
            return True
        return session.grant.scope != session.grant.SCOPE_READ_ONLY


class IsSupportAgent(BasePermission):
    """Staff flag *and* a SupportAgent row. Either alone is not an agent.

    Refused outright for an impersonated request: the desk is where an
    agent works tickets, and an impersonation token is the client's
    identity, not the agent's.
    """

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated or not user.is_staff:
            return False
        if session_from_request(request) is not None:
            return False
        return hasattr(user, "support_agent")

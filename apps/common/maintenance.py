"""Reading the maintenance switch, and enforcing it.

The switch is a singleton row an operator flips in Django admin. This
module is the only thing that reads it, so the rules about who gets
through live in one place.

Cached in the shared cache backend, not a module global. The same reason
``extraction_config`` does: the web process and the RQ worker are
different processes, and a switch that only reached one of them would be
a switch that does not work. A short TTL backs the explicit invalidation
up so a process that misses the delete still converges.

**A missing table is not an outage.** Before the migration runs, or if
the database is unreachable, this reports "not in maintenance". The
alternative — failing closed — would mean a database blip takes the
product offline and then prevents anyone reading the row that would let
them turn it back on.
"""

from __future__ import annotations

import logging

from django.core.cache import cache
from django.db import OperationalError, ProgrammingError
from django.http import JsonResponse

logger = logging.getLogger(__name__)

_CACHE_KEY = "maintenance_mode:v1"
_CACHE_TTL = 15  # seconds — an operator is watching this take effect

#: Path prefixes that keep working while maintenance is on.
#:
#: ``/admin/`` is the important one: without it the person who turned
#: maintenance on cannot turn it off, and the feature becomes a way to
#: take the product down for good.
_ALWAYS_ALLOWED = (
    "/admin/",
    "/static/",
    "/media/",
    "/api/v1/auth/",          # an operator must be able to sign in
    "/api/v1/maintenance/",   # the frontend asks "are we down?"
)

#: The Tally connector's own feed, gated separately by a checkbox on the
#: row. The TCP polls on a timer from a client's machine.
_TALLY_CONNECTOR_PREFIXES = (
    "/api/v1/tally/connector/",
    "/api/v1/tally/ledgers/",
    "/api/v1/tally/masters/",
)


def _read_row():
    from apps.common.models import MaintenanceMode
    return MaintenanceMode.objects.filter(pk=MaintenanceMode.SINGLETON_PK).first()


def get_maintenance_state(force_refresh: bool = False) -> dict:
    """Current switch state as a plain dict, never raising."""
    if not force_refresh:
        cached = cache.get(_CACHE_KEY)
        if cached is not None:
            return cached

    state = {
        "is_active": False,
        "message": "",
        "expected_back_at": None,
        "allow_tally_connector": True,
    }
    try:
        row = _read_row()
        if row is not None:
            state = {
                "is_active": bool(row.is_active),
                "message": row.message or "",
                "expected_back_at": (
                    row.expected_back_at.isoformat() if row.expected_back_at else None
                ),
                "allow_tally_connector": bool(row.allow_tally_connector),
            }
    except (OperationalError, ProgrammingError) as exc:
        # Table not migrated yet, or the database is unreachable. Serve.
        logger.debug("Maintenance row unreadable (%s) — treating as off", exc)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Maintenance row read failed (%s) — treating as off", exc)

    cache.set(_CACHE_KEY, state, _CACHE_TTL)
    return state


def invalidate_maintenance_cache():
    cache.delete(_CACHE_KEY)


def _is_staff(user) -> bool:
    return bool(
        user is not None
        and getattr(user, "is_authenticated", False)
        and (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))
    )


def _user_from_jwt(request):
    """The user behind this request's bearer token, or None.

    Cheap when there is no Authorization header, which is the common
    case for the static and admin paths that reach this.
    """
    header = request.META.get("HTTP_AUTHORIZATION") or ""
    if not header.lower().startswith("bearer "):
        return None
    try:
        from rest_framework_simplejwt.authentication import JWTAuthentication

        result = JWTAuthentication().authenticate(request)
        return result[0] if result else None
    except Exception:
        # An expired or malformed token is not staff, and is not this
        # middleware's problem to report — the view will say so.
        return None


def _is_exempt(request, state) -> bool:
    """Whether this request goes through despite maintenance being on."""
    path = request.path or "/"

    if path.startswith(_ALWAYS_ALLOWED):
        return True

    # Staff and superusers always pass, so the team can verify a release
    # against production before letting clients back in.
    #
    # `request.user` alone is not enough here. Middleware runs before DRF
    # does its authentication, and `AuthenticationMiddleware` only reads
    # the *session*. Every API call carries a JWT instead, so at this
    # point `request.user` is AnonymousUser for exactly the requests the
    # exemption is meant to cover — staff were being locked out of their
    # own maintenance window. So the token is resolved here too.
    if _is_staff(getattr(request, "user", None)):
        return True
    if _is_staff(_user_from_jwt(request)):
        return True

    if state["allow_tally_connector"] and path.startswith(_TALLY_CONNECTOR_PREFIXES):
        return True

    return False


class MaintenanceModeMiddleware:
    """Refuse ordinary requests with 503 while maintenance is on.

    503 with ``Retry-After`` is the correct status: it tells browsers,
    monitors and search engines that this is temporary, where a 500
    would look like a fault and a 200 with an error body would look like
    success.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        state = get_maintenance_state()

        if not state["is_active"] or _is_exempt(request, state):
            return self.get_response(request)

        return JsonResponse(
            {
                "detail": state["message"] or (
                    "Bill Munshi is briefly down for maintenance. "
                    "Your data is safe — please try again shortly."
                ),
                "maintenance": True,
                "expected_back_at": state["expected_back_at"],
            },
            status=503,
            headers={"Retry-After": "300"},
        )

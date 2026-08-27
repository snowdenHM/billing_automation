"""Tally TCP bridge health-check endpoints.

Two paired endpoints:

* ``POST /api/v1/tally/org/<org_id>/health/ping/`` — the Tally TDL /
  TCP connector hits this every 10 minutes with the org API key. Each
  hit stamps ``TallyConfig.last_tally_ping_at`` and returns a small
  JSON body the connector can log ({"pong": true, ...}).

* ``GET /api/v1/tally/org/<org_id>/health/`` — the frontend polls this
  (typically every 30-60 s) and renders a green/red connectivity badge
  in the Account Info card. Returns:
    {
      "connected": bool,
      "last_ping_at": ISO8601 or null,
      "seconds_since_ping": int or null,
      "threshold_seconds": 900   # 10-min interval + 5-min grace
    }

  ``connected`` is ``True`` iff a ping arrived within
  ``threshold_seconds``. A never-pinged config returns ``connected:
  False`` with null timestamp.
"""

import logging

from django.utils import timezone
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.permissions import IsOrgAdmin
from apps.common.utils import get_organization_from_request

from ..models import TallyConfig
from .helpers import OrganizationAPIKeyOrBearerToken

logger = logging.getLogger(__name__)


# Ping cadence contract: TCP bridge pings every 10 minutes; give an
# extra 5 minutes of grace so a single missed cycle (network blip,
# Tally briefly closed) doesn't flip the badge red.
PING_INTERVAL_SECONDS = 10 * 60
PING_THRESHOLD_SECONDS = 15 * 60


def _get_or_create_config(organization):
    """TallyConfig may not exist yet for a fresh org — create the row
    on first ping so the timestamp always has a home."""
    config, _created = TallyConfig.objects.get_or_create(organization=organization)
    return config


@extend_schema(
    summary="Tally TCP bridge health ping",
    description=(
        "Called by the Tally TCP / TDL connector every 10 minutes. "
        "Stamps the org's ``last_tally_ping_at`` and returns a small "
        "pong body so the connector can log the round-trip."
    ),
    responses={
        200: OpenApiResponse(description="Pong — ping recorded."),
        404: OpenApiResponse(description="Organization not found."),
    },
    tags=['Tally Health'],
)
@api_view(['POST', 'GET'])
@permission_classes([OrganizationAPIKeyOrBearerToken])
def tally_health_ping(request, org_id):
    """Record a health ping from the Tally TCP bridge.

    Accepts both POST (canonical) and GET (fallback for TDL setups
    where issuing a POST is awkward). Either way the effect is the
    same: stamp ``last_tally_ping_at`` = now.
    """
    organization = get_organization_from_request(request, org_id)
    if not organization:
        return Response(
            {
                'error': 'Organization Access Denied',
                'message': f'Organization with ID {org_id} not found or you do not have access to it.',
                'error_code': 'ORG_NOT_FOUND',
            },
            status=status.HTTP_404_NOT_FOUND,
        )

    now = timezone.now()
    config = _get_or_create_config(organization)
    config.last_tally_ping_at = now
    config.save(update_fields=['last_tally_ping_at'])

    return Response(
        {
            'pong': True,
            'organization_id': str(organization.id),
            'server_time': now.isoformat(),
            'next_ping_in_seconds': PING_INTERVAL_SECONDS,
        },
        status=status.HTTP_200_OK,
    )


@extend_schema(
    summary="Tally TCP bridge health status",
    description=(
        "Frontend polls this endpoint to render the Account Info "
        "connectivity badge. Returns whether the last ping arrived "
        "within the threshold, plus the raw timestamp."
    ),
    responses={
        200: OpenApiResponse(description="Health status returned."),
        404: OpenApiResponse(description="Organization not found."),
    },
    tags=['Tally Health'],
)
@api_view(['GET'])
@permission_classes([IsAuthenticated, IsOrgAdmin])
def tally_health_status(request, org_id):
    """Report Tally TCP connectivity for the given org."""
    organization = get_organization_from_request(request, org_id)
    if not organization:
        return Response(
            {
                'error': 'Organization Access Denied',
                'message': f'Organization with ID {org_id} not found or you do not have access to it.',
                'error_code': 'ORG_NOT_FOUND',
            },
            status=status.HTTP_404_NOT_FOUND,
        )

    config = TallyConfig.objects.filter(organization=organization).first()
    last_ping = getattr(config, 'last_tally_ping_at', None) if config else None

    if last_ping is None:
        return Response(
            {
                'connected': False,
                'last_ping_at': None,
                'seconds_since_ping': None,
                'threshold_seconds': PING_THRESHOLD_SECONDS,
                'ping_interval_seconds': PING_INTERVAL_SECONDS,
                'message': 'Tally TCP bridge has never pinged this org.',
            },
            status=status.HTTP_200_OK,
        )

    seconds_since = int((timezone.now() - last_ping).total_seconds())
    connected = seconds_since <= PING_THRESHOLD_SECONDS

    return Response(
        {
            'connected': connected,
            'last_ping_at': last_ping.isoformat(),
            'seconds_since_ping': seconds_since,
            'threshold_seconds': PING_THRESHOLD_SECONDS,
            'ping_interval_seconds': PING_INTERVAL_SECONDS,
            'message': (
                'Tally TCP bridge is online.'
                if connected
                else f'No ping in the last {seconds_since // 60} minute(s).'
            ),
        },
        status=status.HTTP_200_OK,
    )

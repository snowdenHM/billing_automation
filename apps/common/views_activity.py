"""Reading the bill activity log.

One endpoint serves both uses. Without filters it is the organisation-wide
feed; with ``?bill_id=`` it is one bill's timeline. Bill ids are UUIDs and
unique across every module, so a single route covers Tally and Zoho alike.
"""

import logging

from django.utils.dateparse import parse_datetime
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.models import BillActivity
from apps.common.pagination import DefaultPagination
from apps.common.utils import get_organization_from_request

logger = logging.getLogger(__name__)


class BillActivitySerializer(serializers.ModelSerializer):
    action_label = serializers.CharField(source="get_action_display", read_only=True)
    bill_type_label = serializers.CharField(source="get_bill_type_display", read_only=True)
    actor_email = serializers.EmailField(source="actor.email", read_only=True, default=None)

    class Meta:
        model = BillActivity
        fields = [
            "id", "module", "bill_type", "bill_type_label", "bill_id",
            "action", "action_label", "actor_label", "actor_email",
            "summary", "changes", "metadata", "created_at",
        ]
        read_only_fields = fields


@extend_schema(
    summary="Bill activity log",
    description=(
        "Activity for one bill (`bill_id`) or the whole organisation. "
        "Newest first."
    ),
    parameters=[
        OpenApiParameter("bill_id", str, description="Limit to one bill's timeline."),
        OpenApiParameter("module", str, description="tally | zoho"),
        OpenApiParameter("bill_type", str, description="vendor | expense | payment | journal"),
        OpenApiParameter("action", str, description="Repeatable; comma-separated also accepted."),
        OpenApiParameter("actor", str, description="User id."),
        OpenApiParameter("since", str, description="ISO datetime, inclusive."),
        OpenApiParameter("until", str, description="ISO datetime, inclusive."),
    ],
    responses={200: BillActivitySerializer(many=True)},
    tags=["Activity"],
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def bill_activity_list(request, org_id):
    organization = get_organization_from_request(request, org_id)
    if not organization:
        return Response(
            {"detail": "Organization not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    qs = (
        BillActivity.objects
        .filter(organization=organization)
        .select_related("actor")
    )

    params = request.query_params

    bill_id = params.get("bill_id")
    if bill_id:
        try:
            qs = qs.filter(bill_id=bill_id)
        except (ValueError, TypeError):
            return Response(
                {"detail": "Invalid bill_id."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    for field in ("module", "bill_type"):
        value = params.get(field)
        if value:
            qs = qs.filter(**{field: value})

    # Accepts ?action=verified&action=synced and ?action=verified,synced.
    actions = [
        a for raw in params.getlist("action") for a in raw.split(",") if a
    ]
    if actions:
        qs = qs.filter(action__in=actions)

    actor = params.get("actor")
    if actor:
        qs = qs.filter(actor_id=actor)

    since, until = params.get("since"), params.get("until")
    if since:
        parsed = parse_datetime(since)
        if parsed:
            qs = qs.filter(created_at__gte=parsed)
    if until:
        parsed = parse_datetime(until)
        if parsed:
            qs = qs.filter(created_at__lte=parsed)

    paginator = DefaultPagination()
    page = paginator.paginate_queryset(qs, request)
    serializer = BillActivitySerializer(page if page is not None else qs, many=True)
    if page is not None:
        return paginator.get_paginated_response(serializer.data)
    return Response(serializer.data)

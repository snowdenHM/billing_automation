"""Reading the bill activity log.

One endpoint serves both uses. Without filters it is the organisation-wide
feed; with ``?bill_id=`` it is one bill's timeline. Bill ids are UUIDs and
unique across every module, so a single route covers Tally and Zoho alike.
"""

import logging
from collections import Counter, defaultdict
from datetime import timedelta

from django.utils import timezone
from django.utils.dateparse import parse_datetime
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common import roles
from apps.common.models import BillActivity
from apps.common.permissions import HasOrgPermission, requires
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


@extend_schema(
    summary="Extraction and correction statistics",
    description=(
        "Aggregates of the activity log: which route read the bills, and "
        "which fields people keep correcting afterwards. The second is the "
        "live accuracy signal — a field nobody corrects is a field the "
        "extractor gets right."
    ),
    parameters=[OpenApiParameter("days", int, description="Window, default 30.")],
    tags=["Activity"],
)
@requires(roles.MANAGE_SETTINGS)
@api_view(["GET"])
@permission_classes([IsAuthenticated, HasOrgPermission])
def activity_stats(request, org_id):
    """Extraction quality for the whole organisation, across every module.

    Admin-only, and that is the point of it. The numbers here are how
    often our own extractor was wrong, which is an engineering
    instrument, not a customer-facing one: a client reading "invoice
    number corrected 40% of the time" has one question, and it is not a
    question about bills. It is also a noisy measure — a derived field
    recomputing, or somebody reformatting a date, both count as
    corrections — so it overstates error and should never be quoted to
    anyone outside the team.

    Not scoped to a module on purpose: the events come from Tally and
    Zoho alike, and extraction quality is a property of the extractor,
    not of whichever accounting package a bill is bound for. The
    per-module split below is there so a difference between them is
    visible rather than averaged away.
    """
    organization = get_organization_from_request(request, org_id)
    if not organization:
        return Response({"detail": "Organization not found."},
                        status=status.HTTP_404_NOT_FOUND)

    try:
        days = max(1, min(int(request.query_params.get("days", 30)), 365))
    except (TypeError, ValueError):
        days = 30
    since = timezone.now() - timedelta(days=days)

    events = BillActivity.objects.filter(organization=organization, created_at__gte=since)

    # --- how bills were read -------------------------------------------
    routes, models, confidences, shadow_same, shadow_differ = Counter(), Counter(), [], Counter(), Counter()
    for meta in events.filter(action=BillActivity.Action.EXTRACTED).values_list("metadata", flat=True):
        meta = meta or {}
        routes[meta.get("route") or "unknown"] += 1
        models[meta.get("model") or "unknown"] += 1
        if isinstance(meta.get("ocr_confidence"), (int, float)):
            confidences.append(meta["ocr_confidence"])
        agreement = meta.get("agreement") or {}
        for f in agreement.get("same") or []:
            shadow_same[f] += 1
        for f in agreement.get("differ") or []:
            shadow_differ[f] += 1

    # --- what people corrected afterwards -------------------------------
    # A field changed at verify is a field the extractor got wrong. Counted
    # against how many verifies happened, not against how many changes, so
    # the rate means "how often this field needs fixing".
    corrections, verifies = Counter(), 0
    verified_events = events.filter(action=BillActivity.Action.VERIFIED)
    for changes in verified_events.values_list("changes", flat=True):
        verifies += 1
        for change in changes or []:
            field = change.get("field")
            if field:
                corrections[field] += 1

    # Split by module so Tally and Zoho are visible separately rather
    # than averaged into one number that describes neither.
    by_module = {}
    for module, _ in BillActivity.Module.choices:
        module_events = events.filter(module=module)
        module_verifies = module_events.filter(action=BillActivity.Action.VERIFIED).count()
        module_corrections = Counter()
        for changes in module_events.filter(
            action=BillActivity.Action.VERIFIED
        ).values_list("changes", flat=True):
            for change in changes or []:
                if change.get("field"):
                    module_corrections[change["field"]] += 1
        by_module[module] = {
            "uploaded": module_events.filter(
                action=BillActivity.Action.UPLOADED).count(),
            "verifies": module_verifies,
            "corrections": sum(module_corrections.values()),
            "clean_rate": (
                round(1 - min(1, sum(module_corrections.values()) / module_verifies), 3)
                if module_verifies else None
            ),
        }

    return Response({
        "window_days": days,
        "since": since.isoformat(),
        "counts": dict(Counter(events.values_list("action", flat=True))),
        "extraction": {
            "total": sum(routes.values()),
            "routes": dict(routes),
            "models": dict(models),
            "mean_ocr_confidence": (
                round(sum(confidences) / len(confidences), 3) if confidences else None
            ),
        },
        # Only populated while shadow mode is running.
        "shadow_agreement": [
            {"field": f,
             "same": shadow_same.get(f, 0),
             "differ": shadow_differ.get(f, 0),
             "agreement": round(shadow_same.get(f, 0)
                                / max(1, shadow_same.get(f, 0) + shadow_differ.get(f, 0)), 3)}
            for f in sorted(set(shadow_same) | set(shadow_differ))
        ],
        "by_module": by_module,
        "verifies": verifies,
        "corrections": [
            {"field": f, "count": c,
             "rate": round(c / verifies, 3) if verifies else None}
            for f, c in corrections.most_common()
        ],
    })

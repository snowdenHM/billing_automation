"""Foreign-exchange endpoints: read today's rates, set a bill's currency.

Two routes, deliberately separate from the bill modules. The rate table
is the same for every voucher type and every module, so it does not
belong inside one of them; and putting the "set the currency on this
bill" write here too means the five Tally verify views — each already
over two thousand lines — do not each grow another branch.
"""

import logging
from decimal import Decimal, InvalidOperation

from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common import roles
from apps.common.models import BillActivity
from apps.common.permissions import HasOrgPermission, requires
from apps.common.services import forex
from apps.common.services.activity import record_activity
from apps.common.utils import get_organization_from_request

logger = logging.getLogger(__name__)


def _analysed_models():
    """The analysed headers that carry currency, imported late."""
    from apps.module.tally.models import (
        TallyExpenseAnalyzedBill, TallyPaymentAnalyzedBill,
        TallyReceiptAnalyzedBill, TallySalesAnalyzedBill,
        TallyVendorAnalyzedBill,
    )

    return (
        TallyVendorAnalyzedBill, TallyExpenseAnalyzedBill,
        TallyPaymentAnalyzedBill, TallySalesAnalyzedBill,
        TallyReceiptAnalyzedBill,
    )


def _find_analysed(bill_id, organization):
    """The analysed header for an uploaded bill, whichever type it is.

    Looked up by the parent bill's id rather than its own, because that
    is the id the detail page already has in its URL.
    """
    for model in _analysed_models():
        row = model.objects.filter(
            selected_bill_id=bill_id, organization=organization,
        ).first()
        if row is not None:
            return row
    return None


# ---------------------------------------------------------------------------
# Rates
# ---------------------------------------------------------------------------

class ForexRatesSerializer(serializers.Serializer):
    base = serializers.CharField()
    rates = serializers.DictField(child=serializers.CharField())
    fetched_at = serializers.CharField(allow_null=True)
    provider = serializers.CharField(allow_null=True)
    source = serializers.CharField()
    stale = serializers.BooleanField()
    currencies = serializers.ListField(child=serializers.DictField())


@extend_schema(
    summary="Live INR exchange rates",
    description=(
        "Rates are **INR per one unit** of each currency. Cached in Redis and "
        "shared across processes; the provider is not called per bill. When "
        "the provider is unreachable the last cached rates are returned with "
        "`stale: true` and the `fetched_at` they were captured at, so the "
        "screen can say how old they are instead of blocking the operator."
    ),
    parameters=[
        OpenApiParameter(
            "refresh", bool,
            description="Bypass the fresh cache and re-ask the provider.",
        ),
    ],
    responses={200: ForexRatesSerializer},
    tags=["Forex"],
)
@requires(roles.VIEW_BILLS)
@api_view(["GET"])
@permission_classes([IsAuthenticated, HasOrgPermission])
def forex_rates(request, org_id):
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    refresh = str(request.query_params.get("refresh", "")).lower() in ("1", "true", "yes")

    # The currency list is served alongside the rates so the dropdown has
    # exactly one source of truth: anything the screen offers is something
    # this server will accept.
    currencies = [
        {"code": code, "label": forex.CURRENCY_LABELS.get(code, code)}
        for code in forex.SUPPORTED_CURRENCIES
    ]

    try:
        payload = forex.get_rates(force_refresh=refresh)
    except forex.ForexUnavailable as exc:
        # 200, not 5xx. There is genuinely no rate, but the operator can
        # still type one by hand, and a failed request would hide that
        # option behind an error state.
        logger.warning("Forex rates unavailable for org %s: %s", organization.id, exc)
        return Response({
            "base": forex.BASE_CURRENCY,
            "rates": {forex.BASE_CURRENCY: "1.000000"},
            "fetched_at": None,
            "provider": None,
            "source": "unavailable",
            "stale": True,
            "detail": str(exc),
            "currencies": currencies,
        })

    return Response({**payload, "currencies": currencies})


# ---------------------------------------------------------------------------
# A bill's currency
# ---------------------------------------------------------------------------

class BillCurrencySerializer(serializers.Serializer):
    currency = serializers.CharField(max_length=3)
    # Optional. Left out, the server freezes today's rate; sent, the
    # operator's own number wins and is recorded as hand-entered.
    exchange_rate = serializers.CharField(required=False, allow_blank=True)


@extend_schema(
    summary="Set a bill's currency and freeze its exchange rate",
    description=(
        "Stores the currency **and the rate used** on the analysed bill. The "
        "rate is frozen at this moment and never re-derived: a voucher posted "
        "in March at 83.20 must still reconcile in June when the rate is 85. "
        "Omit `exchange_rate` to take the current live rate; send one to "
        "override it by hand (recorded as `manual`)."
    ),
    request=BillCurrencySerializer,
    responses={200: BillCurrencySerializer},
    tags=["Forex"],
)
@requires(roles.EDIT_BILLS)
@api_view(["PATCH"])
@permission_classes([IsAuthenticated, HasOrgPermission])
def bill_currency(request, org_id, bill_id):
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    serializer = BillCurrencySerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    code = serializer.validated_data["currency"].upper()

    if not forex.is_supported(code):
        return Response(
            {"detail": f"{code} is not a supported currency.",
             "supported": list(forex.SUPPORTED_CURRENCIES)},
            status=status.HTTP_400_BAD_REQUEST,
        )

    analysed = _find_analysed(bill_id, organization)
    if analysed is None:
        return Response(
            {"detail": "No analysed bill found for that id. Analyse the bill first."},
            status=status.HTTP_404_NOT_FOUND,
        )

    previous = (analysed.currency or "INR", analysed.exchange_rate)
    manual = serializer.validated_data.get("exchange_rate", "").strip()

    if code == forex.BASE_CURRENCY:
        # Going back to rupees resets the rate rather than leaving a
        # stale 83.2 on a bill now marked INR, which would double every
        # figure on it at sync time.
        rate = Decimal("1.000000")
        source = analysed.RateSource.BASE
        fetched_at = None
    elif manual:
        try:
            rate = Decimal(manual)
        except (InvalidOperation, ValueError):
            return Response({"detail": f"'{manual}' is not a number."},
                            status=status.HTTP_400_BAD_REQUEST)
        if rate <= 0:
            return Response({"detail": "Exchange rate must be greater than zero."},
                            status=status.HTTP_400_BAD_REQUEST)
        source = analysed.RateSource.MANUAL
        fetched_at = timezone.now()
    else:
        try:
            rate, meta = forex.rate_for(code)
        except forex.ForexUnavailable as exc:
            # No live rate and none typed: say so and let the operator
            # supply one, rather than guessing or silently storing 1.
            return Response(
                {"detail": f"No rate available for {code} ({exc}). Enter one by hand.",
                 "code": "rate_unavailable"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        source = (
            analysed.RateSource.CACHED if meta.get("stale")
            else analysed.RateSource.LIVE
        )
        fetched_at = timezone.now()

    analysed.currency = code
    analysed.exchange_rate = rate
    analysed.exchange_rate_source = source
    analysed.exchange_rate_at = fetched_at
    analysed.save(update_fields=[
        "currency", "exchange_rate", "exchange_rate_source", "exchange_rate_at",
    ])

    # Worth a line in the history: it silently rescales every figure the
    # accounting system will receive, and is otherwise invisible on the
    # bill once set.
    parent = getattr(analysed, "selected_bill", None)
    if parent is not None and (previous[0] != code or previous[1] != rate):
        record_activity(
            parent, BillActivity.Action.EDITED,
            actor=request.user,
            summary=(
                "Currency set to INR"
                if code == forex.BASE_CURRENCY
                else f"Currency set to {code} at {rate} INR"
            ),
            changes={"currency": {"old": previous[0], "new": code},
                     "exchange_rate": {"old": str(previous[1]), "new": str(rate)}},
        )

    return Response({
        "bill_id": str(bill_id),
        "currency": code,
        "exchange_rate": str(rate),
        "exchange_rate_source": source,
        "exchange_rate_at": analysed.exchange_rate_at,
    })

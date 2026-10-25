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
from apps.common.services import cbic, forex
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


# ---------------------------------------------------------------------------
# Which CBIC column a voucher type belongs on
# ---------------------------------------------------------------------------
# CBIC notifies two rates per currency, and they are not close: on USD
# under notification 29/2026 the import rate is 95.90 and the export rate
# 94.20, a 1.8% gap. Reading the wrong column is a real misstatement, not
# a rounding difference.
#
# Three of the five voucher types settle it on their own:
#
#   Purchase (vendor bill) and Expense   goods and services coming in  -> import
#   Sales                                goods and services going out  -> export
#
# Payment and Receipt do not, and the mapping below is a *default*, not a
# determination. A payment voucher is usually settling an import and a
# receipt usually realising an export, which is why they are mapped the
# way they are — but a payment can be a refund to a customer and a
# receipt an advance from one, and nothing on the voucher says which.
#
# So this never decides alone. The screen shows both columns with this
# side preselected, and the PATCH below records whichever one the
# operator actually clicked. The default saves four clicks out of five;
# it is not allowed to be the last word.
_VOUCHER_SIDES = {
    "TallyVendorAnalyzedBill": cbic.SIDE_IMPORT,
    "TallyExpenseAnalyzedBill": cbic.SIDE_IMPORT,
    "TallyPaymentAnalyzedBill": cbic.SIDE_IMPORT,
    "TallySalesAnalyzedBill": cbic.SIDE_EXPORT,
    "TallyReceiptAnalyzedBill": cbic.SIDE_EXPORT,
}

def _default_side(analysed):
    return _VOUCHER_SIDES.get(type(analysed).__name__, cbic.SIDE_IMPORT)


def _cbic_block():
    """The notified-rate half of the rates response. Never raises.

    Returned as its own block rather than merged into ``rates`` so the
    screen cannot accidentally treat a notified rate as a market one, or
    the reverse, by reading the wrong key.
    """
    try:
        payload = cbic.get_notified_rates()
    except cbic.CbicUnavailable as exc:
        return {
            "available": False,
            "rates": {},
            "notification": None,
            "effective_from": None,
            "fetched_at": None,
            "provider": cbic.PROVIDER_NAME,
            "source": "unavailable",
            "stale": True,
            "detail": str(exc),
        }
    except Exception as exc:  # pragma: no cover - belt and braces
        logger.warning("CBIC block failed unexpectedly: %s", exc)
        return {
            "available": False, "rates": {}, "notification": None,
            "effective_from": None, "fetched_at": None,
            "provider": cbic.PROVIDER_NAME, "source": "unavailable",
            "stale": True, "detail": str(exc),
        }
    return {"available": True, **payload}


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
    cbic = serializers.DictField()


@extend_schema(
    summary="INR exchange rates — market and CBIC notified",
    description=(
        "Rates are **INR per one unit** of each currency. Cached in Redis and "
        "shared across processes; the provider is not called per bill. When "
        "the provider is unreachable the last cached rates are returned with "
        "`stale: true` and the `fetched_at` they were captured at, so the "
        "screen can say how old they are instead of blocking the operator."
        "\n\n"
        "Two rate sets come back and they are **not interchangeable**:\n\n"
        "* `rates` — a market mid-rate. Indicative. Right for management "
        "reporting and for sanity-checking a document.\n"
        "* `cbic` — the rate notified by CBIC under section 14 of the Customs "
        "Act, 1962, with separate `import` and `export` columns, the "
        "notification number it was published under and the date it took "
        "effect. This is the figure customs valuation and GST on imports "
        "require. If the notified source is unreachable, `cbic.available` is "
        "false and the market rate and hand entry are still there."
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

    # Fetched unconditionally and independently of the market rates: the
    # two providers fail separately, and losing one must not hide the
    # other.
    notified = _cbic_block()

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
            "cbic": notified,
        })

    return Response({**payload, "currencies": currencies, "cbic": notified})


# ---------------------------------------------------------------------------
# A bill's currency
# ---------------------------------------------------------------------------

#: What the caller may ask for. ``cbic`` without a side lets the server
#: resolve the column from the voucher type; ``cbic_import`` and
#: ``cbic_export`` state it outright, and that is what the screen sends,
#: because the operator saw both columns and picked one.
_RATE_SOURCES = ("", "market", "cbic", "cbic_import", "cbic_export")


class BillCurrencySerializer(serializers.Serializer):
    currency = serializers.CharField(max_length=3)
    # Optional. Left out, the server freezes today's rate; sent, the
    # operator's own number wins and is recorded as hand-entered.
    exchange_rate = serializers.CharField(required=False, allow_blank=True)
    # Optional, and absent means "market" so every existing caller keeps
    # working unchanged.
    rate_source = serializers.ChoiceField(
        choices=_RATE_SOURCES, required=False, allow_blank=True,
    )


@extend_schema(
    summary="Set a bill's currency and freeze its exchange rate",
    description=(
        "Stores the currency **and the rate used** on the analysed bill. The "
        "rate is frozen at this moment and never re-derived: a voucher posted "
        "in March at 83.20 must still reconcile in June when the rate is 85."
        "\n\n"
        "Which rate is frozen depends on what is sent:\n\n"
        "* `exchange_rate` present — that number wins, recorded as `manual`.\n"
        "* `rate_source: cbic_import` / `cbic_export` — the CBIC notified "
        "rate off that column, recorded with the notification it came from.\n"
        "* `rate_source: cbic` — the notified rate off whichever column the "
        "voucher type implies (purchase and expense import, sales export).\n"
        "* anything else, including nothing — the market rate, as before.\n\n"
        "The response carries `exchange_rate_reference`, the notification "
        "citation, which is empty for market and hand-entered rates."
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

    previous = (
        analysed.currency or "INR",
        analysed.exchange_rate,
        analysed.exchange_rate_source or "",
    )
    manual = serializer.validated_data.get("exchange_rate", "").strip()
    requested = (serializer.validated_data.get("rate_source") or "").strip()
    reference = ""

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
    elif requested.startswith("cbic"):
        # The statutory path. The side comes from the request when the
        # operator chose one and from the voucher type only as a
        # fallback; either way it is recorded, because "the CBIC rate"
        # without a column does not identify a number.
        side = {
            "cbic_import": cbic.SIDE_IMPORT,
            "cbic_export": cbic.SIDE_EXPORT,
        }.get(requested) or _default_side(analysed)

        # The notification in force on the bill's own date, not
        # today's. CBIC re-notifies roughly fortnightly, so a bill
        # entered three weeks late would otherwise be valued on a
        # notification that did not exist when it was raised — which is
        # not the figure an assessment uses. Bills with no date yet fall
        # back to the current notification.
        as_at = getattr(analysed, "bill_date", None)

        try:
            rate, meta = cbic.notified_rate_for(code, side, as_at=as_at)
        except (cbic.CbicUnavailable, ValueError) as exc:
            # Specifically does not fall back to the market rate. The
            # operator asked for a statutory figure; quietly handing
            # them an indicative one is the failure this endpoint is
            # most obliged not to produce. They can read it off the
            # notification and type it.
            return Response(
                {"detail": (
                    f"No CBIC notified {side} rate available for {code}"
                    f"{f' as at {as_at}' if as_at else ''} ({exc}). "
                    "Enter it by hand from the notification."
                ),
                 "code": "notified_rate_unavailable"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        source = (
            analysed.RateSource.CBIC_IMPORT if side == cbic.SIDE_IMPORT
            else analysed.RateSource.CBIC_EXPORT
        )
        reference = cbic.reference_for(meta)
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
    analysed.exchange_rate_reference = reference
    analysed.exchange_rate_at = fetched_at
    analysed.save(update_fields=[
        "currency", "exchange_rate", "exchange_rate_source",
        "exchange_rate_reference", "exchange_rate_at",
    ])

    # Worth a line in the history: it silently rescales every figure the
    # accounting system will receive, and is otherwise invisible on the
    # bill once set.
    parent = getattr(analysed, "selected_bill", None)
    # Also logged when only the *source* moved: swapping a market rate
    # for the notified one changes nothing an operator can see on the
    # bill but changes everything about whether the voucher stands up in
    # a return, so it must not be an invisible edit.
    if parent is not None and (
        previous[0] != code or previous[1] != rate or previous[2] != str(source)
    ):
        record_activity(
            parent, BillActivity.Action.EDITED,
            actor=request.user,
            summary=(
                "Currency set to INR"
                if code == forex.BASE_CURRENCY
                else f"Currency set to {code} at {rate} INR"
                     + (f" ({reference})" if reference else f" ({source})")
            ),
            changes={"currency": {"old": previous[0], "new": code},
                     "exchange_rate": {"old": str(previous[1]), "new": str(rate)},
                     "exchange_rate_source": {"old": previous[2], "new": str(source)}},
        )

    return Response({
        "bill_id": str(bill_id),
        "currency": code,
        "exchange_rate": str(rate),
        "exchange_rate_source": source,
        "exchange_rate_reference": reference,
        "exchange_rate_at": analysed.exchange_rate_at,
    })

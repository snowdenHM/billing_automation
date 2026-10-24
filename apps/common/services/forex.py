"""Live foreign-exchange rates against INR, and the rate a bill was posted at.

Two separate things live here, and keeping them separate is the point of
the module:

  * **Today's rates.** Fetched from a public provider, cached in Redis,
    and shared by every bill and every process. Nobody calls the
    provider per bill.

  * **The rate a bill used.** Not stored here — stored *on the bill*
    (``currency`` / ``exchange_rate`` / ``exchange_rate_at`` /
    ``exchange_rate_source``). This module only hands out a number for
    the operator to freeze onto the bill; once frozen it is never
    re-derived.

That second point is the whole design. A bill that records only "USD"
and looks the rate up again at read time silently rewrites its own
history: a voucher posted in March at 83.20 reconciles at 85.00 in June
and the books stop balancing against what was actually paid. So the
rate travels with the bill, and this module is never consulted when
reading one back.

Provider
--------
``open.er-api.com`` (the free, keyless tier of ExchangeRate-API). Chosen
because it needs no key and no billing relationship, covers every
currency the sheet asks for including AED — which the obvious
alternative, the ECB feed behind frankfurter.app, does not publish — and
reports its own ``time_last_update_unix`` so we can show the operator how
old a number is rather than implying it is live.

Its limits, which the operator is shown and which matter:

  * It updates roughly **once a day**, not intraday. A rate shown at
    15:00 is that morning's.
  * It is a **market mid-rate**. For Indian statutory purposes the
    correct figure is often the CBIC notified rate (customs) or the RBI
    reference rate, which this is not and will not match to the paisa.
    That is why the rate is editable by hand on every bill.
  * It is a free service with no contractual uptime. Everything below
    is written so an outage degrades the screen rather than blocking
    the operator.
"""

import logging
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal, InvalidOperation

import requests
from django.core.cache import cache

logger = logging.getLogger(__name__)

#: The currencies sheet task 29 asks for, plus the base. "YEN" on the
#: sheet is JPY; there is no ISO code "YEN".
SUPPORTED_CURRENCIES = ("INR", "USD", "EUR", "GBP", "CAD", "JPY", "AUD", "AED", "SGD")

BASE_CURRENCY = "INR"

#: Human labels, so the dropdown does not have to keep its own copy and
#: drift from what the server will accept.
CURRENCY_LABELS = {
    "INR": "Indian Rupee",
    "USD": "US Dollar",
    "EUR": "Euro",
    "GBP": "Pound Sterling",
    "CAD": "Canadian Dollar",
    "JPY": "Japanese Yen",
    "AUD": "Australian Dollar",
    "AED": "UAE Dirham",
    "SGD": "Singapore Dollar",
}

_PROVIDER_URL = "https://open.er-api.com/v6/latest/INR"
_PROVIDER_NAME = "open.er-api.com"
_HTTP_TIMEOUT = 10

#: Fresh copy. Short enough that a mid-day correction by the provider is
#: picked up, long enough that a busy org makes a handful of calls a day.
_FRESH_KEY = "forex:rates:inr"
_FRESH_TTL = 6 * 60 * 60

#: Last-known-good copy, kept far longer than the fresh one. This is what
#: makes an outage survivable: when the provider is unreachable the
#: operator gets yesterday's number clearly labelled as stale, instead of
#: an empty box and a blocked bill.
_STALE_KEY = "forex:rates:inr:last_good"
_STALE_TTL = 30 * 24 * 60 * 60

#: Rates are stored to six places. Several of these currencies quote
#: below 1 INR (JPY is ~0.55), so two places would round a yen invoice
#: into nonsense.
_RATE_PLACES = Decimal("0.000001")


class ForexUnavailable(Exception):
    """No rate could be produced at all — not even a stale cached one."""


def _quantize(value):
    try:
        return Decimal(str(value)).quantize(_RATE_PLACES)
    except (InvalidOperation, ValueError, TypeError):
        return None


def _fetch_from_provider():
    """Today's rates as ``{code: Decimal INR-per-unit}``, or None.

    The provider quotes *foreign units per 1 INR* because we ask it for
    an INR base; a bill needs the other direction — how many rupees one
    dollar is — so every quote is inverted here, once, rather than at
    each of the dozen places that would otherwise have to remember to.
    """
    response = requests.get(_PROVIDER_URL, timeout=_HTTP_TIMEOUT)
    response.raise_for_status()
    body = response.json()

    if body.get("result") != "success":
        raise ValueError(f"provider returned result={body.get('result')!r}")

    quotes = body.get("rates") or {}
    rates = {}
    for code in SUPPORTED_CURRENCIES:
        if code == BASE_CURRENCY:
            rates[code] = Decimal("1.000000")
            continue
        per_inr = quotes.get(code)
        try:
            per_inr = Decimal(str(per_inr))
        except (InvalidOperation, ValueError, TypeError):
            continue
        if per_inr <= 0:
            continue
        rates[code] = (Decimal("1") / per_inr).quantize(_RATE_PLACES)

    if len(rates) < 2:
        raise ValueError("provider returned no usable quotes")

    updated = body.get("time_last_update_unix")
    try:
        fetched_at = datetime.fromtimestamp(int(updated), tz=dt_timezone.utc)
    except (TypeError, ValueError):
        fetched_at = datetime.now(tz=dt_timezone.utc)

    return {
        "base": BASE_CURRENCY,
        "rates": {code: str(rate) for code, rate in rates.items()},
        "fetched_at": fetched_at.isoformat(),
        "provider": _PROVIDER_NAME,
    }


def get_rates(*, force_refresh=False):
    """Current INR rates, with provenance. Never raises for an outage.

    Returns a dict ``{base, rates, fetched_at, provider, source, stale}``
    where ``source`` is one of:

      ``live``   – just fetched from the provider.
      ``cache``  – the fresh cached copy, under six hours old.
      ``stale``  – the provider is unreachable and this is the last good
                   copy we ever saw. ``stale`` is True and ``fetched_at``
                   says how old it is, so the screen can say so.

    Raises ``ForexUnavailable`` only when there is nothing at all to
    return — the provider is down *and* nothing was ever cached.
    """
    if not force_refresh:
        cached = cache.get(_FRESH_KEY)
        if cached:
            return {**cached, "source": "cache", "stale": False}

    try:
        payload = _fetch_from_provider()
    except Exception as exc:
        # Deliberately broad: a DNS failure, a TLS error, a 503 and a
        # provider that changed its JSON shape are the same event here —
        # "no fresh number" — and all of them must fall through to the
        # stale copy rather than reach the operator as a 500.
        logger.warning("Forex provider %s unavailable: %s", _PROVIDER_NAME, exc)
        last_good = cache.get(_STALE_KEY)
        if last_good:
            return {**last_good, "source": "stale", "stale": True}
        raise ForexUnavailable(
            f"{_PROVIDER_NAME} is unreachable and no rate has ever been cached"
        ) from exc

    cache.set(_FRESH_KEY, payload, _FRESH_TTL)
    cache.set(_STALE_KEY, payload, _STALE_TTL)
    return {**payload, "source": "live", "stale": False}


def rate_for(currency, *, force_refresh=False):
    """How many INR one unit of *currency* is worth right now.

    Returns ``(Decimal, meta)``. INR short-circuits to exactly 1 without
    touching the cache or the network — the overwhelmingly common case
    is a rupee bill, and it should cost nothing.
    """
    code = (currency or BASE_CURRENCY).upper()
    if code == BASE_CURRENCY:
        return Decimal("1.000000"), {
            "source": "base", "stale": False, "fetched_at": None,
            "provider": None,
        }

    payload = get_rates(force_refresh=force_refresh)
    raw = (payload.get("rates") or {}).get(code)
    rate = _quantize(raw)
    if rate is None or rate <= 0:
        raise ForexUnavailable(f"No rate available for {code}")

    return rate, {
        "source": payload.get("source"),
        "stale": payload.get("stale", False),
        "fetched_at": payload.get("fetched_at"),
        "provider": payload.get("provider"),
    }


def is_supported(currency):
    return (currency or "").upper() in SUPPORTED_CURRENCIES


# ---------------------------------------------------------------------------
# Converting a bill's amounts for posting
# ---------------------------------------------------------------------------
# Amounts are stored on the bill in the currency written on the document,
# because that is what the operator is reading off the paper in front of
# them and checking against. Conversion happens once, at the edge, when
# the voucher is handed to an accounting system that only speaks INR.

#: Money is emitted to Tally with two decimal places; see the existing
#: payload builders, which format every amount this way.
_MONEY_PLACES = Decimal("0.01")


def to_inr(amount, exchange_rate):
    """*amount*, in the bill's currency, as INR at the bill's frozen rate.

    Returns a ``Decimal`` rounded to paise, or None when the input was
    not a number. A rate of 1 is returned unchanged rather than
    re-quantised, so an INR bill's figures are bit-for-bit what they
    always were.
    """
    if amount is None or amount == "":
        return None
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError, TypeError):
        return None

    try:
        rate = Decimal(str(exchange_rate if exchange_rate not in (None, "") else "1"))
    except (InvalidOperation, ValueError, TypeError):
        rate = Decimal("1")

    if rate == 1:
        return value
    return (value * rate).quantize(_MONEY_PLACES)


def _convert_scalar(text, rate):
    """Scale one already-formatted money string in a sync payload."""
    converted = to_inr(text, rate)
    if converted is None:
        return text
    return f"{converted:.2f}"


#: Keys in a prepared sync-payload bill dict whose values are money.
#: Listed explicitly rather than detected, because a payload also carries
#: numbers that are *not* money — ``quantity`` above all, which must not
#: be multiplied by an exchange rate.
_BILL_MONEY_KEYS = ("total_amount", "amount")
_ITEM_MONEY_KEYS = ("price", "amount")
_LEDGER_MONEY_KEYS = ("amount",)


def convert_sync_payload_to_inr(data, exchange_rate):
    """Scale every money figure in one prepared sync-bill dict into INR.

    Mutates and returns *data*. **No key is added or removed and no key
    order changes** — the Tally connector's XML is shape-sensitive and
    this must stay a pure value substitution. A rate of 1, which is what
    every existing bill has, is a no-op and returns the dict untouched.

    ``quantity`` and ``rate`` (the GST percentage, e.g. ``"18%"``) are
    deliberately not in the money lists: multiplying either by an
    exchange rate would be silently, expensively wrong.
    """
    try:
        rate = Decimal(str(exchange_rate if exchange_rate not in (None, "") else "1"))
    except (InvalidOperation, ValueError, TypeError):
        return data
    if rate == 1 or not isinstance(data, dict):
        return data

    for key in _BILL_MONEY_KEYS:
        if key in data:
            data[key] = _convert_scalar(data[key], rate)

    for entry in data.get("ledgers") or []:
        if not isinstance(entry, dict):
            continue
        for key in _LEDGER_MONEY_KEYS:
            if key in entry:
                entry[key] = _convert_scalar(entry[key], rate)

    for item in data.get("items") or []:
        if not isinstance(item, dict):
            continue
        for key in _ITEM_MONEY_KEYS:
            if key in item:
                item[key] = _convert_scalar(item[key], rate)

    return data

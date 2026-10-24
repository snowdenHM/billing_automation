"""CBIC notified exchange rates — the statutory figure, not a market one.

Why this exists alongside ``forex.py``
--------------------------------------
``forex.py`` serves a market mid-rate. That is the right number for
management reporting and for a quick sanity check against the document
in front of the operator. It is the *wrong* number the moment the figure
lands in a statutory return.

For customs valuation under section 14 of the Customs Act, 1962 — and
therefore for the assessable value that GST on imports is charged on —
the rate that governs is the one **notified by the Central Board of
Indirect Taxes and Customs**. It is not a mid-rate, it is not today's
market, and it will not match ``open.er-api.com`` to the paisa. An
auditor asking "what rate did you use?" wants a notification number, not
a provider name.

Where the numbers come from
---------------------------
Since 4 July 2024, CBIC publishes these through the **Exchange Rate
Automation Module (ERAM)**: rates sourced from SBI, rounded to the
nearest five paise, pushed into ICES, and published on ICEGATE. They
change on a notification cycle — nominally the evenings of the 1st and
3rd Thursdays, effective from the following midnight, plus an off-cycle
re-notification whenever a currency moves more than 5% in a day.

This module reads them from the JSON service that backs ICEGATE's own
public *View Exchange Rate* screen, the one ``cbic.gov.in`` links to:

    POST {BASE}/icegateapi/igexratesubscribe
         {"currencyCode": "ALL", "currencyDate": "DD-MM-YYYY"}
      -> {"status": 1, "notStartDate": ..., "notEndDate": "15-10-2026",
          "currencyDetail": [{"currencyCode": "USD", "currencyDesc": ...,
                              "cbicImport": 95.9, "cbicExport": 94.2,
                              "units": "1.0"}, ...]}

    POST {BASE}/icegateapi/getnotdetails
      -> [{"notificationNumber": "29/2026", "notPublishDate": "15-10-2026"}, ...]

How much this can be trusted — read before relying on it
--------------------------------------------------------
Two different questions, and they have different answers.

*Are the numbers right?* Yes, as far as anything can be. This is the
first-party government service; the same data drives customs assessment.
It is not a mirror, not a scrape of a PDF, and not a third party's
reading of a notification. The notification number comes back with it,
so what we store is citable.

*Is the interface stable?* **No promise whatsoever.** It is the private
backend of an Angular app, not a documented API. There is no published
contract, no versioning, no uptime commitment, no terms of use that
mention programmatic access, and nothing stops ICEGATE renaming a field
or putting the route behind a session tomorrow. The browser screen in
front of it has a captcha (client-side only, and not enforced here).

So this is written as a *convenience*, never as a dependency:

  * every failure is a ``None``, never an exception reaching a view;
  * a last-known-good copy survives 30 days of outage;
  * the operator can always type the rate off the notification by hand,
    and that path must keep working if this module is deleted outright.

Treat a silent break as expected, not exceptional. If the shape changes,
the screen loses a button and nobody is blocked.
"""

import logging
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal, InvalidOperation

import requests
from django.core.cache import cache

logger = logging.getLogger(__name__)

#: The ICEGATE front-office "common business utility" service — the base
#: the public View Exchange Rate screen posts to.
_BASE = "https://foservices.icegate.gov.in/cbu/icegateapi"
_RATES_URL = f"{_BASE}/igexratesubscribe"
_NOTIFICATIONS_URL = f"{_BASE}/getnotdetails"

PROVIDER_NAME = "icegate.gov.in (CBIC ERAM)"

_HTTP_TIMEOUT = 10

_FRESH_KEY = "forex:cbic:rates"
_FRESH_TTL = 6 * 60 * 60

_STALE_KEY = "forex:cbic:rates:last_good"
_STALE_TTL = 30 * 24 * 60 * 60

#: Same six places as the market rates, for the same reason: JPY is
#: notified per 100 units and divides down to well under one rupee.
_RATE_PLACES = Decimal("0.000001")

#: The two columns CBIC publishes. They are not interchangeable and the
#: difference is material — around 1.8% on USD — so the side a bill used
#: is recorded, not inferred at read time.
SIDE_IMPORT = "import"
SIDE_EXPORT = "export"
SIDES = (SIDE_IMPORT, SIDE_EXPORT)


class CbicUnavailable(Exception):
    """No notified rate could be produced — not even a stale cached one."""


def _quantize(value):
    try:
        return Decimal(str(value)).quantize(_RATE_PLACES)
    except (InvalidOperation, ValueError, TypeError):
        return None


def _per_unit(published, units):
    """INR for **one** unit of the currency.

    CBIC notifies JPY and KRW per 100 units. Posting 61.20 as the rate
    for one yen would multiply a Japanese invoice by a hundred, so the
    division happens here, once, and what leaves this module is always
    per-unit — the same convention ``exchange_rate`` on a bill uses.
    """
    rate = _quantize(published)
    if rate is None or rate <= 0:
        return None
    try:
        divisor = Decimal(str(units or "1"))
    except (InvalidOperation, ValueError, TypeError):
        divisor = Decimal("1")
    if divisor <= 0:
        divisor = Decimal("1")
    return (rate / divisor).quantize(_RATE_PLACES)


def _fetch_notification_number(effective_from):
    """The notification this rate set was published under, e.g. ``29/2026``.

    Best effort and deliberately non-fatal: the rates are usable without
    it, they are merely less citable. Matched on the publish date the
    rates call hands back; falling back to the most recent notification
    not in the future, which is what the screen shows.
    """
    try:
        response = requests.post(_NOTIFICATIONS_URL, timeout=_HTTP_TIMEOUT)
        response.raise_for_status()
        rows = response.json() or []
    except Exception as exc:
        logger.info("CBIC notification list unavailable: %s", exc)
        return None

    if not isinstance(rows, list):
        return None

    def _parse(text):
        try:
            return datetime.strptime(text, "%d-%m-%Y").date()
        except (TypeError, ValueError):
            return None

    if effective_from:
        for row in rows:
            if isinstance(row, dict) and row.get("notPublishDate") == effective_from:
                return row.get("notificationNumber")

    today = date.today()
    best = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        when = _parse(row.get("notPublishDate"))
        if when is None or when > today:
            continue
        if best is None or when > best[0]:
            best = (when, row.get("notificationNumber"))
    return best[1] if best else None


def _fetch():
    """One full notified rate set, or raise.

    A single ``ALL`` call covers all 22 notified currencies, so this is
    one request however many currencies the org uses — the per-currency
    route the screen uses would be 22.
    """
    payload = {
        "currencyCode": "ALL",
        "currencyDate": date.today().strftime("%d-%m-%Y"),
    }
    response = requests.post(_RATES_URL, json=payload, timeout=_HTTP_TIMEOUT)
    response.raise_for_status()
    body = response.json()

    if not isinstance(body, dict):
        raise ValueError("CBIC service returned a non-object body")
    if body.get("error"):
        raise ValueError(f"CBIC service returned error={body.get('error')!r}")

    rows = body.get("currencyDetail") or []
    rates = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = (row.get("currencyCode") or "").upper()
        if not code:
            continue
        units = row.get("units")
        imp = _per_unit(row.get("cbicImport"), units)
        exp = _per_unit(row.get("cbicExport"), units)
        if imp is None and exp is None:
            continue
        rates[code] = {
            SIDE_IMPORT: str(imp) if imp is not None else None,
            SIDE_EXPORT: str(exp) if exp is not None else None,
            "units": str(units or "1"),
            "label": row.get("currencyDesc") or code,
        }

    if len(rates) < 2:
        raise ValueError("CBIC service returned no usable rates")

    # ``notEndDate`` is the publish date of the notification in force;
    # ``notStartDate`` is the first day it applies, and the two differ by
    # a day on the ALL query. The publish date is what the notification
    # list is keyed on, so that is what we match with.
    effective_from = body.get("notEndDate") or body.get("notStartDate")

    return {
        "rates": rates,
        "effective_from": effective_from,
        "notification": _fetch_notification_number(effective_from),
        "fetched_at": datetime.now(tz=dt_timezone.utc).isoformat(),
        "provider": PROVIDER_NAME,
    }


def get_notified_rates(*, force_refresh=False):
    """The notified rate set, with provenance. Never raises for an outage.

    Returns ``{rates, effective_from, notification, fetched_at, provider,
    source, stale}`` where ``source`` is ``live``, ``cache`` or ``stale``
    — the same vocabulary ``forex.get_rates`` uses, so a screen can treat
    the two blocks alike.

    ``stale`` means something different here than it does for a market
    rate, and the difference matters. A market rate from yesterday is
    *wrong*. A notified rate from a fortnight ago is *current*: CBIC
    rates stand until superseded. So staleness here is only a statement
    about our last successful fetch, and ``notification`` plus
    ``effective_from`` is what actually says whether the number is the
    one in force.

    Raises ``CbicUnavailable`` only when there is nothing at all.
    """
    if not force_refresh:
        cached = cache.get(_FRESH_KEY)
        if cached:
            return {**cached, "source": "cache", "stale": False}

    try:
        payload = _fetch()
    except Exception as exc:
        # Broad on purpose, and this is the module's whole posture: an
        # undocumented government endpoint will fail in ways nobody
        # enumerated, and every one of them must come out as "no
        # notified rate today" rather than a 500 on a bill screen.
        logger.warning("CBIC rates unavailable from %s: %s", PROVIDER_NAME, exc)
        last_good = cache.get(_STALE_KEY)
        if last_good:
            return {**last_good, "source": "stale", "stale": True}
        raise CbicUnavailable(
            f"{PROVIDER_NAME} is unreachable and no notified rate has ever been cached"
        ) from exc

    cache.set(_FRESH_KEY, payload, _FRESH_TTL)
    cache.set(_STALE_KEY, payload, _STALE_TTL)
    return {**payload, "source": "live", "stale": False}


def notified_rate_for(currency, side, *, force_refresh=False):
    """INR per one unit of *currency* on the import or export column.

    Returns ``(Decimal, meta)``. *side* must be ``import`` or ``export``;
    there is no default, because defaulting would be exactly the silent
    guess this whole split exists to prevent.
    """
    code = (currency or "").upper()
    if side not in SIDES:
        raise ValueError(f"side must be one of {SIDES!r}, got {side!r}")

    payload = get_notified_rates(force_refresh=force_refresh)
    entry = (payload.get("rates") or {}).get(code) or {}
    rate = _quantize(entry.get(side))
    if rate is None or rate <= 0:
        raise CbicUnavailable(f"CBIC does not notify a {side} rate for {code}")

    return rate, {
        "source": payload.get("source"),
        "stale": payload.get("stale", False),
        "fetched_at": payload.get("fetched_at"),
        "provider": payload.get("provider"),
        "notification": payload.get("notification"),
        "effective_from": payload.get("effective_from"),
        "side": side,
    }


def reference_for(meta):
    """A one-line citation to store on the bill, e.g. ``CBIC 29/2026 w.e.f. 15-10-2026``.

    This is the string an auditor reads off the voucher. It is built from
    what the service returned rather than assembled on the screen, so a
    rate and its citation cannot drift apart.
    """
    notification = meta.get("notification")
    effective = meta.get("effective_from")
    if notification and effective:
        return f"CBIC {notification} w.e.f. {effective}"
    if notification:
        return f"CBIC {notification}"
    if effective:
        return f"CBIC notified w.e.f. {effective}"
    return "CBIC notified"

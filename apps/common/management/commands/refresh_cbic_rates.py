"""Pull the current CBIC notified exchange rates into the cache.

Nothing breaks without this: every screen fetches on demand and falls
back to a 30-day last-known-good copy. What it buys is timing and
warning.

Timing, because the live cache holds for six hours. CBIC normally
notifies fortnightly, but re-notifies off-cycle when a currency moves
more than 5% in a day, and a bill valued in the six-hour gap after such
a notification carries the superseded figure. Running this each evening
closes that window to the length of one working session.

Warning, because the ICEGATE endpoint behind this is the private backend
of a government web app — undocumented, unversioned, with nothing
promising it will keep its shape. A silent break would otherwise surface
weeks later as a stale citation on a voucher. This command exits
non-zero when it cannot fetch, so whatever watches cron sees it the same
night.

    # crontab -e — 7:30pm daily, after CBIC's usual publishing window
    30 19 * * * cd /path/to/billing_automation && \
        /path/to/.venv/bin/python manage.py refresh_cbic_rates \
        >> /var/log/billmunshi/cbic-refresh.log 2>&1

Historical lookups (the notification in force on a bill's own date) are
cached separately for 30 days and are not touched here: a past
notification cannot change, so there is nothing to refresh.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.common.services import cbic


class Command(BaseCommand):
    help = "Fetch today's CBIC notified exchange rates and warm the cache."

    def add_arguments(self, parser):
        parser.add_argument(
            "--check",
            action="store_true",
            help=(
                "Report what is cached without calling CBIC. Exits non-zero "
                "if the cached copy is stale."
            ),
        )

    def handle(self, *args, **options):
        if options["check"]:
            try:
                payload = cbic.get_notified_rates()
            except cbic.CbicUnavailable as exc:
                raise CommandError(f"No notified rate cached at all: {exc}")
            self._report(payload)
            if payload.get("stale"):
                raise CommandError(
                    "Cached notified rates are stale — the last fetch failed."
                )
            return

        try:
            payload = cbic.get_notified_rates(force_refresh=True)
        except cbic.CbicUnavailable as exc:
            # A hard failure, not a warning: the point of the command is
            # to be the thing that notices.
            raise CommandError(f"Could not refresh CBIC rates: {exc}")

        self._report(payload)
        if payload.get("stale"):
            raise CommandError(
                "CBIC is unreachable; serving the last known good notification."
            )

    def _report(self, payload):
        rates = payload.get("rates") or {}
        self.stdout.write(
            f"Notification {payload.get('notification') or '?'} "
            f"w.e.f. {payload.get('effective_from') or '?'} "
            f"— {len(rates)} currencies, source={payload.get('source')}"
        )
        usd = rates.get("USD") or {}
        if usd:
            self.stdout.write(
                f"  USD  import {usd.get('import')}  export {usd.get('export')}"
            )

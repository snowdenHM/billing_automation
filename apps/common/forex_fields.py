"""The currency a voucher was written in, and the rate it was posted at.

An abstract mixin rather than four copied field blocks, because the one
thing that must never differ between voucher types is how a bill records
its own money.

Why the rate is stored on the row
---------------------------------
The alternative — store ``currency`` alone and look the rate up when the
bill is read — is the obvious design and it is wrong. It makes a posted
voucher mutable: a March purchase recorded at 83.20 silently becomes
85.00 in June, the INR figure in the books no longer matches the INR
figure that was posted to Tally, and nothing in the system records that
anything changed. Freezing the rate onto the bill at the moment the
operator accepts it is what makes the voucher reconcile months later.

``exchange_rate`` is **INR per one unit of** ``currency``. Amounts on the
bill itself stay in ``currency`` — what the document says — and are
converted at the edge, when the voucher is handed to an accounting
system. Defaults of ``INR`` and ``1.000000`` mean every bill that
existed before this shipped converts to itself.
"""

from decimal import Decimal

from django.db import models


class ForeignCurrencyMixin(models.Model):
    class RateSource(models.TextChoices):
        BASE = "base", "Rupee bill"
        LIVE = "live", "Market rate (live)"
        CACHED = "cached", "Market rate (cached)"
        MANUAL = "manual", "Entered by hand"
        # CBIC's notified rate, split by the column it was read from.
        # Imports and exports are notified at different figures — about
        # 1.8% apart on USD — so "CBIC" alone would not tell an auditor
        # which number was used. The side is part of the answer.
        CBIC_IMPORT = "cbic_import", "CBIC notified (imports)"
        CBIC_EXPORT = "cbic_export", "CBIC notified (exports)"

    #: The source values that mean "a statutory notified rate". Grouped
    #: here so a caller asking "is this good enough for a return?" does
    #: not have to know the spelling of each one.
    CBIC_SOURCES = ("cbic_import", "cbic_export")

    currency = models.CharField(
        max_length=3,
        default="INR",
        help_text="ISO code of the currency written on the document.",
    )

    # Six decimal places because several supported currencies quote below
    # one rupee — JPY is around 0.55 — and two places would round a yen
    # invoice into a different number.
    exchange_rate = models.DecimalField(
        max_digits=18,
        decimal_places=6,
        default=Decimal("1.000000"),
        help_text="INR per one unit of `currency`, frozen when the operator accepted it.",
    )

    exchange_rate_source = models.CharField(
        max_length=20,
        choices=RateSource.choices,
        default=RateSource.BASE,
        help_text="Where this rate came from, so a hand-typed one is never mistaken for a fetched one.",
    )

    # The citation, when there is one: "CBIC 29/2026 w.e.f. 15-10-2026".
    # A notified rate is only as good as the notification it came from —
    # "we used the CBIC rate" is not an answer to an auditor, "we used
    # the rate notified under 29/2026" is. Blank for market and
    # hand-entered rates, which have no notification to cite.
    exchange_rate_reference = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="The notification this rate was published under, if it was a notified rate.",
    )

    # Nullable, and deliberately not auto-populated: a row with
    # ``exchange_rate_at = NULL`` is one that has never left the INR
    # default, which is different from one fetched at a known moment.
    exchange_rate_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the stored rate was fetched or entered.",
    )

    class Meta:
        abstract = True

    @property
    def is_foreign_currency(self):
        return (self.currency or "INR").upper() != "INR"

    @property
    def uses_notified_rate(self):
        """True when the frozen rate is a CBIC notified one.

        The distinction a return cares about: a market mid-rate and a
        notified rate are both "a rate", and only one of them is the
        figure a customs or GST-on-imports computation is allowed to use.
        """
        return self.exchange_rate_source in self.CBIC_SOURCES

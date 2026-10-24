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
        LIVE = "live", "Live rate"
        CACHED = "cached", "Cached rate"
        MANUAL = "manual", "Entered by hand"

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
        max_length=10,
        choices=RateSource.choices,
        default=RateSource.BASE,
        help_text="Where this rate came from, so a hand-typed one is never mistaken for a fetched one.",
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

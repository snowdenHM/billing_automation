"""An 18% tax must not post to a ledger named for 5%.

Reported from a live sales voucher on 2026-11-15:

    <ledger><amount>58500.00</amount><ledger>SALES @18%</ledger></ledger>
    <ledger><amount>10530.00</amount><ledger>Output IGST 5%</ledger>
            <rate>18%</rate></ledger>

10530 / 58500 is exactly 18%, and the payload says so in ``<rate>``. The
ledger it posts to says 5%. The amount is right and the GST position is
wrong, which is the worst shape for this kind of error: nothing in the
voucher looks amiss, and Tally's rate-wise summary — what GSTR-1 is
filed from — is wrong by the whole line.

Two causes, one on top of the other:

* ``find_tally_tax_ledger`` picked ``name__icontains('igst').first()``
  with no idea of the rate. An organisation with "Output IGST 5%",
  "Output IGST 12%" and "Output IGST 18%" got whichever the database
  returned first, for every rate.
* ``GstRateLedgerMapping`` — the table the setup page's "GST rate
  mappings" section writes — was read by nothing at all. The operator
  could map 18% to the right ledger and be ignored.

The third time in this project that configuration was saved, displayed
and never read.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.module.tally.models import (
    GstRateLedgerMapping, Ledger, ParentLedger, TallyConfig,
)
from apps.module.tally.views.helpers import find_tally_tax_ledger
from apps.organizations.models import Organization

User = get_user_model()


class TaxLedgerRateTests(TestCase):
    """The client's chart of accounts: one IGST ledger per slab."""

    @classmethod
    def setUpTestData(cls):
        owner = User.objects.create_user(
            email="rates@tally.test", password="x-test-pass-123",
        )
        cls.org = Organization.objects.create(
            name="Rates Org", owner=owner, created_by=owner,
        )
        cls.duties = ParentLedger.objects.create(
            organization=cls.org, parent="Duties & Taxes",
        )
        # Created 5% first on purpose — it is what an unordered query
        # returns, and it is the ledger the live bill wrongly used.
        cls.igst_5 = Ledger.objects.create(
            organization=cls.org, name="Output IGST 5%", parent=cls.duties,
        )
        cls.igst_12 = Ledger.objects.create(
            organization=cls.org, name="Output IGST 12%", parent=cls.duties,
        )
        cls.igst_18 = Ledger.objects.create(
            organization=cls.org, name="Output IGST 18%", parent=cls.duties,
        )

        cfg = TallyConfig.objects.create(organization=cls.org)
        for field in ("igst_parents", "cgst_parents", "sgst_parents"):
            getattr(cfg, field).set([cls.duties])
        cls.cfg = cfg

    def pick(self, rate=None):
        return find_tally_tax_ledger(
            self.org, 180, 0, 0, ledger_type="bill", rate=rate,
        )

    # -- the bug -------------------------------------------------------
    def test_an_18_percent_tax_picks_the_18_percent_ledger(self):
        self.assertEqual(
            self.pick("18%"), self.igst_18,
            "an 18% tax was posted to a ledger named for another rate",
        )

    def test_a_5_percent_tax_picks_the_5_percent_ledger(self):
        self.assertEqual(self.pick("5%"), self.igst_5)

    def test_a_12_percent_tax_picks_the_12_percent_ledger(self):
        self.assertEqual(self.pick("12%"), self.igst_12)

    def test_the_rate_may_arrive_as_a_number_or_a_string(self):
        for rate in (18, "18", "18%", "18.00", Decimal("18")):
            self.assertEqual(
                self.pick(rate), self.igst_18, f"{rate!r} did not resolve",
            )

    # -- the configured mapping wins -----------------------------------
    def test_a_configured_mapping_beats_the_name(self):
        """The operator's answer outranks our reading of the name."""
        GstRateLedgerMapping.objects.create(
            organization=self.org, rate=Decimal("18.00"),
            igst_ledger=self.igst_12,
        )
        self.assertEqual(
            self.pick("18%"), self.igst_12,
            "the GST rate mapping was ignored, as it always has been",
        )

    def test_a_mapping_for_another_rate_is_not_used(self):
        GstRateLedgerMapping.objects.create(
            organization=self.org, rate=Decimal("5.00"),
            igst_ledger=self.igst_12,
        )
        self.assertEqual(self.pick("18%"), self.igst_18)

    # -- nothing changes when the rate is unknown ----------------------
    def test_without_a_rate_the_old_behaviour_stands(self):
        """Call sites that cannot supply a rate must not start failing."""
        self.assertIsNotNone(self.pick(None))

    def test_an_unknown_rate_still_returns_something(self):
        """A 28% tax in a chart with no 28% ledger must not post nowhere."""
        self.assertIsNotNone(self.pick("28%"))

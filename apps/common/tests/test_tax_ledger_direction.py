"""Output tax must not post to an input-credit ledger.

Client correction, 2026-11-18: "Add one column in each GST ledger
section — CGST Input, CGST Output, SGST Input, SGST Output, IGST Input,
IGST Output."

Asked for as a settings-page column, but the reason it is needed is a
posting bug. GST paid on a purchase is input credit; GST charged on a
sale is output liability; a chart of accounts carries both ("Input IGST
18%", "Output IGST 18%"). ``find_tally_tax_ledger`` had no idea which
it was being asked for, and ``GstRateLedgerMapping`` had one slot per
rate serving sales and purchases alike — so whichever ledger the
organisation mapped was used for both.

That is the same shape as the rate bug in ``test_tax_ledger_rate``: the
figures balance, the voucher looks right, and Tally's GST summary —
what GSTR-1 is filed from — is wrong by the whole line. It is also the
harder half to notice, because an input/output error does not even
disagree with itself arithmetically.

The original three columns stay the input side, so nothing an
organisation has already configured changes meaning.
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


class TaxLedgerDirectionTests(TestCase):
    """A chart of accounts with both sides, as every real one has."""

    @classmethod
    def setUpTestData(cls):
        owner = User.objects.create_user(
            email="direction@tally.test", password="x-test-pass-123",
        )
        cls.org = Organization.objects.create(
            name="Direction Org", owner=owner, created_by=owner,
        )
        duties = ParentLedger.objects.create(
            organization=cls.org, parent="Duties & Taxes",
        )
        # Input created first on purpose: it is what an unordered query
        # returns, and it is the ledger a sale wrongly used.
        cls.igst_in_18 = Ledger.objects.create(
            organization=cls.org, name="Input IGST 18%", parent=duties,
        )
        cls.igst_out_18 = Ledger.objects.create(
            organization=cls.org, name="Output IGST 18%", parent=duties,
        )
        cls.igst_in_5 = Ledger.objects.create(
            organization=cls.org, name="Input IGST 5%", parent=duties,
        )
        cls.igst_out_5 = Ledger.objects.create(
            organization=cls.org, name="Output IGST 5%", parent=duties,
        )
        cfg = TallyConfig.objects.create(organization=cls.org)
        for field in ("igst_parents", "cgst_parents", "sgst_parents"):
            getattr(cfg, field).set([duties])

    def pick(self, rate=None, direction=None):
        return find_tally_tax_ledger(
            self.org, 180, 0, 0, ledger_type="bill",
            rate=rate, direction=direction,
        )

    # -- the bug -------------------------------------------------------
    def test_a_sale_picks_the_output_ledger(self):
        self.assertEqual(
            self.pick("18%", "output"), self.igst_out_18,
            "a sale posted its GST to an input-credit ledger",
        )

    def test_a_purchase_picks_the_input_ledger(self):
        self.assertEqual(
            self.pick("18%", "input"), self.igst_in_18,
            "a purchase posted its GST to an output-liability ledger",
        )

    def test_the_side_and_the_rate_both_hold(self):
        """Getting one right at the cost of the other is not a fix."""
        cases = [
            ("18%", "output", self.igst_out_18),
            ("18%", "input", self.igst_in_18),
            ("5%", "output", self.igst_out_5),
            ("5%", "input", self.igst_in_5),
        ]
        for rate, direction, expected in cases:
            with self.subTest(rate=rate, direction=direction):
                self.assertEqual(self.pick(rate, direction), expected)

    # -- the configured mapping wins -----------------------------------
    def test_the_output_mapping_is_read(self):
        """The new column must not be another setting nothing reads."""
        GstRateLedgerMapping.objects.create(
            organization=self.org, rate=Decimal("18.00"),
            igst_ledger=self.igst_in_18,
            igst_output_ledger=self.igst_out_5,
        )
        self.assertEqual(
            self.pick("18%", "output"), self.igst_out_5,
            "the output column was ignored",
        )
        self.assertEqual(self.pick("18%", "input"), self.igst_in_18)

    def test_an_unset_output_column_falls_back_to_the_input_one(self):
        """An organisation that has not filled it in keeps what it has."""
        GstRateLedgerMapping.objects.create(
            organization=self.org, rate=Decimal("18.00"),
            igst_ledger=self.igst_in_5,
        )
        self.assertEqual(self.pick("18%", "output"), self.igst_in_5)

    # -- nothing regresses ---------------------------------------------
    def test_without_a_direction_a_ledger_is_still_returned(self):
        """Callers that cannot say which side must not start failing."""
        self.assertIsNotNone(self.pick("18%", None))

    def test_a_chart_that_names_neither_side_still_resolves(self):
        """Most of this rests on names; it must degrade, not break."""
        owner = User.objects.create_user(
            email="plain@tally.test", password="x-test-pass-123",
        )
        org = Organization.objects.create(
            name="Plain Org", owner=owner, created_by=owner,
        )
        duties = ParentLedger.objects.create(
            organization=org, parent="Duties & Taxes",
        )
        plain = Ledger.objects.create(
            organization=org, name="IGST 18%", parent=duties,
        )
        cfg = TallyConfig.objects.create(organization=org)
        cfg.igst_parents.set([duties])
        for direction in ("input", "output", None):
            with self.subTest(direction=direction):
                self.assertEqual(
                    find_tally_tax_ledger(
                        org, 180, 0, 0, ledger_type="bill",
                        rate="18%", direction=direction,
                    ),
                    plain,
                )

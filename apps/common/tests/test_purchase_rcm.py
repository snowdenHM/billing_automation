"""Reverse charge on a purchase voucher.

The thing being guarded here is an accounting error that does not look
like one: posting the input credit without the matching liability
leaves the voucher balanced against the party total and the GST
position wrong by the whole tax amount.

So these tests care about two things above all — that an ordinary bill
is completely unaffected, and that a reverse-charge bill which cannot
produce its liability rows refuses to sync rather than posting half of
itself.
"""

from decimal import Decimal

from django.test import TestCase

from apps.module.tally.models import (
    Ledger, ParentLedger, TallyConfig, TallyVendorAnalyzedBill,
    TallyVendorBill,
)
from apps.module.tally.reverse_charge import (
    purchase_rcm_blockers, purchase_rcm_rows,
)
from apps.organizations.models import Organization
from apps.users.models import User


class PurchaseRcmTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            email="rcm@example.com", password="x", first_name="R", last_name="C",
        )
        cls.org = Organization.objects.create(
            name="RCM Co", created_by=cls.user, owner=cls.user,
        )

    def _bill(self, **kwargs):
        bill = TallyVendorBill.objects.create(
            organization=self.org, bill_munshi_name="RCM-1",
        )
        return TallyVendorAnalyzedBill.objects.create(
            organization=self.org, selected_bill=bill,
            cgst=Decimal("900.00"), sgst=Decimal("900.00"),
            **kwargs,
        )

    def _rcm_group(self, *names):
        parent = ParentLedger.objects.create(
            organization=self.org, parent="RCM Payable",
        )
        config, _ = TallyConfig.objects.get_or_create(organization=self.org)
        config.rcm_parents.add(parent)
        for name in names:
            Ledger.objects.create(
                organization=self.org, name=name, parent=parent,
            )
        return parent

    # -- the case that must not change ------------------------------

    def test_an_ordinary_bill_produces_no_rcm_rows(self):
        """Reverse charge off: nothing is added, nothing is blocked."""
        bill = self._bill(is_reverse_charge=False)

        self.assertEqual(purchase_rcm_rows(bill, self.org), [])
        self.assertEqual(purchase_rcm_blockers(bill, self.org), [])

    def test_an_ordinary_bill_is_unaffected_even_with_rcm_ledgers_present(self):
        """A configured RCM group must not leak onto normal bills."""
        self._rcm_group("CGST RCM Payable", "SGST RCM Payable")
        bill = self._bill(is_reverse_charge=False)

        self.assertEqual(purchase_rcm_rows(bill, self.org), [])

    # -- the happy path ----------------------------------------------

    def test_each_tax_head_gets_its_own_liability_row(self):
        """CGST and SGST are owed separately and cannot be lumped."""
        self._rcm_group("CGST RCM Payable", "SGST RCM Payable")
        bill = self._bill(is_reverse_charge=True)

        rows = purchase_rcm_rows(bill, self.org)

        self.assertEqual([r["tax_type"] for r in rows], ["cgst", "sgst"])
        self.assertEqual([r["amount"] for r in rows],
                         [Decimal("900.00"), Decimal("900.00")])
        self.assertIn("CGST", rows[0]["ledger"].name)
        self.assertIn("SGST", rows[1]["ledger"].name)
        self.assertEqual(purchase_rcm_blockers(bill, self.org), [])

    def test_the_liability_mirrors_the_input_credit_exactly(self):
        """Any gap between the two sides is a misstatement, not a rounding."""
        self._rcm_group("RCM Payable")
        bill = self._bill(is_reverse_charge=True)

        rows = purchase_rcm_rows(bill, self.org)

        self.assertEqual(
            sum(r["amount"] for r in rows),
            (bill.cgst or 0) + (bill.sgst or 0) + (bill.igst or 0),
        )

    def test_a_single_combined_rcm_ledger_also_works(self):
        """Not every chart of accounts splits the payable by head."""
        self._rcm_group("RCM Payable")
        bill = self._bill(is_reverse_charge=True)

        rows = purchase_rcm_rows(bill, self.org)

        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["ledger"].name == "RCM Payable" for r in rows))

    def test_inter_state_mirrors_igst_alone(self):
        self._rcm_group("IGST RCM Payable")
        bill = TallyVendorAnalyzedBill.objects.create(
            organization=self.org,
            selected_bill=TallyVendorBill.objects.create(
                organization=self.org, bill_munshi_name="RCM-IGST",
            ),
            igst=Decimal("1800.00"), is_reverse_charge=True,
        )

        rows = purchase_rcm_rows(bill, self.org)

        self.assertEqual([r["tax_type"] for r in rows], ["igst"])
        self.assertEqual(rows[0]["amount"], Decimal("1800.00"))

    # -- refusing rather than posting half a voucher -----------------

    def test_no_rcm_ledger_configured_blocks_the_sync(self):
        """Without a payable ledger the liability has nowhere to go.

        Posting it to the input-credit ledger would cancel the credit
        out instead of recording the debt, so no rows is the right
        answer and the bill must not sync.
        """
        bill = self._bill(is_reverse_charge=True)

        self.assertEqual(purchase_rcm_rows(bill, self.org), [])
        reasons = purchase_rcm_blockers(bill, self.org)
        self.assertEqual(len(reasons), 1)
        self.assertIn("RCM payable ledger", reasons[0])

    def test_reverse_charge_with_no_tax_is_blocked(self):
        """Either the tax is missing or the flag is wrong — both need a look."""
        self._rcm_group("RCM Payable")
        bill = TallyVendorAnalyzedBill.objects.create(
            organization=self.org,
            selected_bill=TallyVendorBill.objects.create(
                organization=self.org, bill_munshi_name="RCM-notax",
            ),
            is_reverse_charge=True,
        )

        reasons = purchase_rcm_blockers(bill, self.org)

        self.assertEqual(len(reasons), 1)
        self.assertIn("no GST", reasons[0])

    def test_an_rcm_group_scoped_to_another_organisation_does_not_count(self):
        """One client's ledgers must never reach another's bill."""
        other = Organization.objects.create(
            name="Other Co", created_by=self.user, owner=self.user,
        )
        parent = ParentLedger.objects.create(
            organization=other, parent="RCM Payable",
        )
        config, _ = TallyConfig.objects.get_or_create(organization=other)
        config.rcm_parents.add(parent)
        Ledger.objects.create(organization=other, name="RCM Payable", parent=parent)

        bill = self._bill(is_reverse_charge=True)

        self.assertEqual(purchase_rcm_rows(bill, self.org), [])
        self.assertTrue(purchase_rcm_blockers(bill, self.org))

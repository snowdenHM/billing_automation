"""The reverse-charge liability goes where the operator says.

Client correction 13: when reverse charge is turned on, duplicate the
GST rows, carry the same amount, and let the user pick the RCM ledger
for the duplicates.

The first two the server already did — ``purchase_rcm_rows`` mirrors
each tax head at its own amount — but it did them at sync time, so
nothing appeared on screen, and it chose the ledger itself from the RCM
parent group. That works when an organisation has exactly one candidate
per head and silently guesses when it has several. An operator with
"RCM Payable CGST" and "RCM Payable CGST (Imports)" had no way to say
which.

The bill now carries the answer (``igst_rcm_taxes`` and friends), bound
to the mirror row on the voucher. The automatic pick stays as the
fallback for a bill where nobody made a choice, so nothing an
organisation relies on today changes.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.module.tally.models import (
    Ledger, ParentLedger, TallyConfig, TallyVendorAnalyzedBill,
)
from apps.module.tally.reverse_charge import (
    purchase_rcm_blockers, purchase_rcm_rows,
)
from apps.organizations.models import Organization

User = get_user_model()


class RcmLedgerChoiceTests(TestCase):
    """An organisation with more than one candidate per tax head."""

    @classmethod
    def setUpTestData(cls):
        owner = User.objects.create_user(
            email="rcmchoice@tally.test", password="x-test-pass-123",
        )
        cls.org = Organization.objects.create(
            name="RCM Choice Org", owner=owner, created_by=owner,
        )
        rcm_parent = ParentLedger.objects.create(
            organization=cls.org, parent="Duties & Taxes",
        )
        # Created first, so it is what an unordered query returns.
        cls.auto_cgst = Ledger.objects.create(
            organization=cls.org, name="RCM Payable CGST", parent=rcm_parent,
        )
        cls.chosen_cgst = Ledger.objects.create(
            organization=cls.org, name="RCM Payable CGST (Imports)",
            parent=rcm_parent,
        )
        cls.auto_sgst = Ledger.objects.create(
            organization=cls.org, name="RCM Payable SGST", parent=rcm_parent,
        )
        config = TallyConfig.objects.create(organization=cls.org)
        config.rcm_parents.set([rcm_parent])

    def bill(self, **kwargs):
        return TallyVendorAnalyzedBill.objects.create(
            organization=self.org, is_reverse_charge=True,
            cgst=Decimal("4500.00"), sgst=Decimal("4500.00"),
            **kwargs,
        )

    def rows(self, bill):
        return {r["tax_type"]: r for r in purchase_rcm_rows(bill, self.org)}

    # -- the operator's answer wins ------------------------------------
    def test_the_chosen_ledger_is_used(self):
        bill = self.bill(cgst_rcm_taxes=self.chosen_cgst)
        self.assertEqual(
            self.rows(bill)["cgst"]["ledger"], self.chosen_cgst,
            "the liability went to a ledger the operator did not choose",
        )

    def test_an_unchosen_head_still_falls_back(self):
        """Choosing one head must not strand the others."""
        bill = self.bill(cgst_rcm_taxes=self.chosen_cgst)
        rows = self.rows(bill)
        self.assertEqual(rows["cgst"]["ledger"], self.chosen_cgst)
        self.assertEqual(rows["sgst"]["ledger"], self.auto_sgst)

    def test_without_a_choice_nothing_changes(self):
        """Every bill that exists today behaves exactly as before."""
        rows = self.rows(self.bill())
        self.assertEqual(rows["cgst"]["ledger"], self.auto_cgst)
        self.assertEqual(rows["sgst"]["ledger"], self.auto_sgst)

    # -- the amounts are the input rows' own ---------------------------
    def test_each_head_mirrors_its_own_amount(self):
        """CGST and SGST are owed to different governments."""
        bill = self.bill()
        rows = self.rows(bill)
        self.assertEqual(rows["cgst"]["amount"], Decimal("4500.00"))
        self.assertEqual(rows["sgst"]["amount"], Decimal("4500.00"))
        self.assertNotIn("igst", rows, "a zero head produced a liability row")

    # -- the guard rails stay up ---------------------------------------
    def test_reverse_charge_off_produces_nothing(self):
        bill = self.bill()
        bill.is_reverse_charge = False
        self.assertEqual(purchase_rcm_rows(bill, self.org), [])
        self.assertEqual(purchase_rcm_blockers(bill, self.org), [])

    def test_a_chosen_ledger_satisfies_the_blocker(self):
        """A bill that can build its rows must not be refused."""
        bill = self.bill(cgst_rcm_taxes=self.chosen_cgst)
        self.assertEqual(purchase_rcm_blockers(bill, self.org), [])

    def test_no_rcm_group_still_refuses(self):
        """Posting the debt to the input ledger would cancel the credit."""
        other_owner = User.objects.create_user(
            email="nogroup@tally.test", password="x-test-pass-123",
        )
        org = Organization.objects.create(
            name="No RCM Group", owner=other_owner, created_by=other_owner,
        )
        TallyConfig.objects.create(organization=org)
        bill = TallyVendorAnalyzedBill.objects.create(
            organization=org, is_reverse_charge=True, cgst=Decimal("90.00"),
        )
        self.assertEqual(purchase_rcm_rows(bill, org), [])
        self.assertTrue(purchase_rcm_blockers(bill, org))

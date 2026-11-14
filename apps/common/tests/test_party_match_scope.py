"""A party lookup must not cross to the other side of the books.

A firm you both buy from and sell to is ordinary in Indian trade, and it
has the same GSTIN on two ledgers: one under Sundry Creditors, one under
Sundry Debtors.

``find_tally_vendor_ledger`` with ``gst_search_scope='all'`` — which the
journal and payment vouchers ask for — searched every ledger in the
organisation by GSTIN before looking at the party's own group, so
whichever row the database returned first won. A payment matched to the
debtor ledger posts the money on the wrong side.

The scoped set is now tried first whatever the scope. The wide search
remains as a fallback, because the journal genuinely references ledgers
outside the vendor group.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.module.tally.models import Ledger, ParentLedger, TallyConfig
from apps.module.tally.views.helpers import find_tally_vendor_ledger
from apps.organizations.models import Organization

User = get_user_model()

GSTIN = "27AABCU9603R1ZX"


class PartyMatchScopeTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        owner = User.objects.create_user(
            email="party@scope.test", password="x-test-pass-123",
        )
        cls.org = Organization.objects.create(
            name="Scope Org", owner=owner, created_by=owner,
        )
        cls.creditors = ParentLedger.objects.create(
            organization=cls.org, parent="Sundry Creditors",
        )
        cls.debtors = ParentLedger.objects.create(
            organization=cls.org, parent="Sundry Debtors",
        )

        # The customer row is created FIRST, so an unordered query that
        # ignores the party group tends to return it.
        cls.as_customer = Ledger.objects.create(
            organization=cls.org, name="UNITED TRADERS (CUSTOMER)",
            parent=cls.debtors, gst_in=GSTIN,
        )
        cls.as_vendor = Ledger.objects.create(
            organization=cls.org, name="UNITED TRADERS",
            parent=cls.creditors, gst_in=GSTIN,
        )

        cfg = TallyConfig.objects.create(organization=cls.org)
        cfg.vendor_parents.set([cls.creditors])
        cfg.customer_parents.set([cls.debtors])

    def test_a_payment_matches_the_vendor_not_the_customer(self):
        """The wide scope the payment voucher uses must still stay on side."""
        found = find_tally_vendor_ledger(
            "UNITED TRADERS", self.org, vendor_gst=GSTIN,
            use_config_threshold=True, gst_search_scope="all",
            fallback_all_ledgers=True,
        )
        self.assertEqual(
            found, self.as_vendor,
            "a payment matched the customer ledger with the same GSTIN — "
            "the money would post on the wrong side of the books",
        )

    def test_a_sale_matches_the_customer_not_the_vendor(self):
        found = find_tally_vendor_ledger(
            "UNITED TRADERS", self.org, vendor_gst=GSTIN,
            gst_search_scope="parents", party_parents_field="customer_parents",
        )
        self.assertEqual(found, self.as_customer)

    def test_a_purchase_matches_the_vendor(self):
        found = find_tally_vendor_ledger(
            "UNITED TRADERS", self.org, vendor_gst=GSTIN,
            gst_search_scope="parents",
        )
        self.assertEqual(found, self.as_vendor)

    def test_the_wide_search_still_reaches_outside_the_group(self):
        """The journal relies on it; narrowing must not break that."""
        other = ParentLedger.objects.create(
            organization=self.org, parent="Indirect Expenses",
        )
        odd = Ledger.objects.create(
            organization=self.org, name="COURIER CO",
            parent=other, gst_in="29AAACC1234F1Z5",
        )
        found = find_tally_vendor_ledger(
            "COURIER CO", self.org, vendor_gst="29AAACC1234F1Z5",
            use_config_threshold=True, gst_search_scope="all",
            fallback_all_ledgers=True,
        )
        self.assertEqual(
            found, odd,
            "the fallback stopped reaching ledgers outside the vendor group",
        )

    def test_a_scoped_search_does_not_reach_outside(self):
        """Purchase and sales must stay inside their group, as before."""
        other = ParentLedger.objects.create(
            organization=self.org, parent="Bank Accounts",
        )
        Ledger.objects.create(
            organization=self.org, name="HDFC CURRENT",
            parent=other, gst_in="19AAACH1234F1Z7",
        )
        found = find_tally_vendor_ledger(
            "HDFC CURRENT", self.org, vendor_gst="19AAACH1234F1Z7",
            gst_search_scope="parents",
        )
        self.assertIsNone(
            found, "a scoped purchase lookup reached a bank ledger",
        )

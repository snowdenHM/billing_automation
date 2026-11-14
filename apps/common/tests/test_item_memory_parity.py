"""Item memory must work the same on a sale as on a purchase.

The purchase side has learned item mappings since the service was
written: the operator resolves "Sadabahar Soyabadi 20 Kg" to
"FORTUNE SOYA 15LTR" once, and every later bill from that vendor
arrives with the item already picked.

The sales side had none of it. ``TallySalesAnalyzedProduct`` was created
without ``item_name`` at all, so the stock item was a manual pick on
every line of every invoice — including for a customer billed the same
goods every week.

Both sides now call the same service against the same table, scoped to
the party ledger on the voucher: the vendor on a purchase, the customer
on a sale.
"""

from django.test import TestCase

from apps.common.services.item_memory import (
    normalise_description, recall_item_choices, remember_item_choice,
)
from apps.module.tally.models import Ledger, ParentLedger, StockItem
from apps.organizations.models import Organization

from django.contrib.auth import get_user_model

User = get_user_model()


class ItemMemoryParityTests(TestCase):
    """One service, both voucher types, no leaks between parties."""

    @classmethod
    def setUpTestData(cls):
        owner = User.objects.create_user(
            email="items@tally.test", password="x-test-pass-123",
        )
        cls.org = Organization.objects.create(
            name="Items Org", owner=owner, created_by=owner,
        )
        debtors = ParentLedger.objects.create(
            organization=cls.org, parent="Sundry Debtors",
        )
        creditors = ParentLedger.objects.create(
            organization=cls.org, parent="Sundry Creditors",
        )
        cls.customer = Ledger.objects.create(
            organization=cls.org, name="ACME TRADERS", parent=debtors,
        )
        cls.vendor = Ledger.objects.create(
            organization=cls.org, name="RELIABLE SUPPLY", parent=creditors,
        )
        cls.item = StockItem.objects.create(
            organization=cls.org, name="FORTUNE SOYA 15LTR",
        )
        cls.other_item = StockItem.objects.create(
            organization=cls.org, name="FORTUNE SOYA 5LTR",
        )

    def recall_row(self, party, description):
        """The remembered mapping row, or None."""
        found = recall_item_choices(
            organization=self.org, party=party, descriptions=[description],
        )
        return found.get(normalise_description(description))

    def recall(self, party, description):
        """Just the stock item half, which most of these tests are about."""
        row = self.recall_row(party, description)
        return row.stock_item if row else None

    def test_a_customer_mapping_is_recalled(self):
        """The sales side learns and recalls, exactly as purchase does."""
        remember_item_choice(
            organization=self.org, party=self.customer,
            description="Sadabahar Soyabadi 20 Kg", stock_item=self.item,
        )
        self.assertEqual(
            self.recall(self.customer, "Sadabahar Soyabadi 20 Kg"), self.item,
        )

    def test_spelling_variation_is_absorbed(self):
        """Case, punctuation and spacing are noise, not a different item."""
        remember_item_choice(
            organization=self.org, party=self.customer,
            description="Sadabahar Soyabadi 20 Kg", stock_item=self.item,
        )
        for spelling in (
            "sadabahar soyabadi - 20kg",
            "SADABAHAR  SOYABADI 20KG",
            "Sadabahar, Soyabadi 20 Kg.",
        ):
            self.assertEqual(
                self.recall(self.customer, spelling), self.item,
                f"{spelling!r} did not recall the mapping",
            )

    def test_a_different_pack_size_is_a_different_item(self):
        """Digits survive normalisation; 20 Kg must not answer for 50 Kg.

        A wrong auto-filled item is worse than an empty one — nobody
        re-reads a field that already looks filled.
        """
        remember_item_choice(
            organization=self.org, party=self.customer,
            description="Sadabahar Soyabadi 20 Kg", stock_item=self.item,
        )
        self.assertIsNone(
            self.recall(self.customer, "Sadabahar Soyabadi 50 Kg"),
        )

    def test_a_mapping_does_not_cross_parties(self):
        """Two parties use the same words for different goods."""
        remember_item_choice(
            organization=self.org, party=self.customer,
            description="Soya Oil", stock_item=self.item,
        )
        self.assertIsNone(
            self.recall_row(self.vendor, "Soya Oil"),
            "a customer's mapping reached a vendor's bill",
        )

    def test_a_mapping_does_not_cross_organisations(self):
        other_owner = User.objects.create_user(
            email="other-items@tally.test", password="x-test-pass-123",
        )
        other_org = Organization.objects.create(
            name="Other Items Org", owner=other_owner, created_by=other_owner,
        )
        remember_item_choice(
            organization=self.org, party=self.customer,
            description="Soya Oil", stock_item=self.item,
        )
        found = recall_item_choices(
            organization=other_org, party=self.customer,
            descriptions=["Soya Oil"],
        )
        self.assertEqual(found, {}, "a mapping leaked to another client")

    def test_the_newest_answer_wins(self):
        """The operator is looking at the bill and we are not."""
        remember_item_choice(
            organization=self.org, party=self.customer,
            description="Soya Oil", stock_item=self.item,
        )
        remember_item_choice(
            organization=self.org, party=self.customer,
            description="Soya Oil", stock_item=self.other_item,
        )
        self.assertEqual(self.recall(self.customer, "Soya Oil"), self.other_item)

    def test_nothing_is_learned_from_an_unanswered_line(self):
        """A line with no stock item chosen teaches nothing."""
        remember_item_choice(
            organization=self.org, party=self.customer,
            description="Soya Oil", stock_item=None,
        )
        self.assertIsNone(self.recall(self.customer, "Soya Oil"))

    def test_both_voucher_views_expose_a_learn_hook(self):
        """Parity is the point; a missing hook means one side stops learning."""
        from apps.module.tally.views import sales_bills, vendor_bills

        for module in (vendor_bills, sales_bills):
            self.assertTrue(
                hasattr(module, "_learn_item_choices"),
                f"{module.__name__} has no _learn_item_choices",
            )


class LineLedgerMemoryTests(ItemMemoryParityTests):
    """The line ledger is the other half of the same answer.

    It used to be chosen by a rate-based heuristic on every bill, and an
    operator who corrected it had to correct it again next month because
    nothing learned from the correction.
    """

    def test_a_line_ledger_is_remembered(self):
        ledger = Ledger.objects.create(
            organization=self.org, name="PURCHASE - OIL",
            parent=ParentLedger.objects.create(
                organization=self.org, parent="Purchase Accounts"),
        )
        remember_item_choice(
            organization=self.org, party=self.customer,
            description="Soya Oil", ledger=ledger,
        )
        row = self.recall_row(self.customer, "Soya Oil")
        self.assertEqual(row.ledger, ledger)
        self.assertIsNone(row.stock_item, "a ledger-only answer invented an item")

    def test_a_ledger_only_answer_is_worth_remembering(self):
        """Inventory sync off means no stock item, every time, for ever."""
        ledger = Ledger.objects.create(
            organization=self.org, name="SALES - OIL",
            parent=ParentLedger.objects.create(
                organization=self.org, parent="Sales Accounts"),
        )
        self.assertIsNotNone(
            remember_item_choice(
                organization=self.org, party=self.customer,
                description="Soya Oil", stock_item=None, ledger=ledger,
            ),
            "a line with a ledger but no stock item taught nothing",
        )

    def test_learning_one_half_does_not_erase_the_other(self):
        """A verify that resolved only the ledger must keep last month's item."""
        ledger = Ledger.objects.create(
            organization=self.org, name="PURCHASE - OIL",
            parent=ParentLedger.objects.create(
                organization=self.org, parent="Purchase Accounts"),
        )
        remember_item_choice(
            organization=self.org, party=self.customer,
            description="Soya Oil", stock_item=self.item,
        )
        remember_item_choice(
            organization=self.org, party=self.customer,
            description="Soya Oil", ledger=ledger,
        )
        row = self.recall_row(self.customer, "Soya Oil")
        self.assertEqual(row.stock_item, self.item, "the item was erased")
        self.assertEqual(row.ledger, ledger)

    def test_remembering_nothing_is_not_a_row(self):
        self.assertIsNone(
            remember_item_choice(
                organization=self.org, party=self.customer,
                description="Soya Oil", stock_item=None, ledger=None,
            )
        )

"""Moving a Zoho bill between voucher types.

The feature exists to keep work that has already been paid for: the
upload, the AI analysis, and whatever the operator mapped by hand. So
these tests are mostly about what must *survive* a move.

The old implementation copied a hand-written list of fields per module.
The vendor selection was on none of those lists, so every move silently
dropped it — along with the duplicate-detection hash and the journal's
ledger mapping. A move that loses the mapping is worse than no move at
all, because the operator has no way to tell it happened.
"""

from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.module.zoho.models import (
    ExpenseBill, ExpenseZohoBill, JournalBill, JournalZohoBill, PaymentBill,
    SalesBill, VendorBill, VendorZohoBill, VendorZohoProduct,
    ZohoChartOfAccount, ZohoCustomer, ZohoTaxes, ZohoVendor,
)
from apps.module.zoho.views.bill_move import move_bill_between_modules_view
from apps.organizations.models import Organization
from apps.users.models import User


class BillMoveTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            email="mv@example.com", password="x", first_name="M", last_name="V",
            is_staff=True, is_superuser=True,
        )
        cls.org = Organization.objects.create(
            name="Move Co", created_by=cls.user, owner=cls.user,
        )

    def setUp(self):
        self.vendor = ZohoVendor.objects.create(
            organization=self.org, contactId="v-1",
            companyName="Acme Traders", gstNo="27AAAAA0000A1Z5",
        )
        self.customer = ZohoCustomer.objects.create(
            organization=self.org, contactId="c-1",
            companyName="Beta Buyers", gstNo="",
        )
        self.account = ZohoChartOfAccount.objects.create(
            organization=self.org, accountId="a-1", accountName="Purchases",
        )
        self.tax = ZohoTaxes.objects.create(
            organization=self.org, taxId="t-1", taxName="GST18",
        )

    def _vendor_bill(self, name="MV-1", **bill_kwargs):
        bill = VendorBill.objects.create(
            organization=self.org, billmunshiName=name,
            status="Analysed", content_hash="hash-abc",
            analysed_data={"vendor": "Acme Traders"},
            **bill_kwargs,
        )
        analysed = VendorZohoBill.objects.create(
            organization=self.org, selectBill=bill, vendor=self.vendor,
            bill_no="INV-9", total="1180", cgst="90", sgst="90",
            note="original note",
        )
        VendorZohoProduct.objects.create(
            organization=self.org, zohoBill=analysed,
            item_details="Widget", amount="1000",
            chart_of_accounts=self.account, taxes=self.tax,
        )
        return bill, analysed

    def _move(self, frm, to, bill_ids):
        request = APIRequestFactory().post(
            "/bills/move/",
            {"from": frm, "to": to, "bill_ids": [str(b) for b in bill_ids]},
            format="json",
        )
        force_authenticate(request, user=self.user)
        return move_bill_between_modules_view(request, str(self.org.id))

    # -- what the feature is for -------------------------------------

    def test_the_vendor_mapping_survives_the_move(self):
        """The field the old implementation dropped on every move."""
        bill, _ = self._vendor_bill()

        response = self._move("vendor", "expense", [bill.id])

        self.assertEqual(response.status_code, 200)
        moved = ExpenseZohoBill.objects.get()
        self.assertEqual(moved.vendor_id, self.vendor.id)

    def test_the_line_items_and_their_mappings_survive(self):
        bill, _ = self._vendor_bill()

        self._move("vendor", "expense", [bill.id])

        line = ExpenseZohoBill.objects.get().products.get()
        self.assertEqual(line.item_details, "Widget")
        self.assertEqual(line.chart_of_accounts_id, self.account.id)
        self.assertEqual(line.taxes_id, self.tax.id)

    def test_the_duplicate_detection_hash_survives(self):
        """Without it the same document can be uploaded again unnoticed."""
        bill, _ = self._vendor_bill()

        self._move("vendor", "expense", [bill.id])

        self.assertEqual(ExpenseBill.objects.get().content_hash, "hash-abc")

    def test_the_analysis_and_status_survive(self):
        bill, _ = self._vendor_bill()

        self._move("vendor", "journal", [bill.id])

        moved = JournalBill.objects.get()
        self.assertEqual(moved.status, "Analysed")
        self.assertEqual(moved.analysed_data, {"vendor": "Acme Traders"})

    def test_the_document_keeps_its_name(self):
        """The name is how an operator refers to the bill."""
        bill, _ = self._vendor_bill()

        self._move("vendor", "expense", [bill.id])

        self.assertEqual(ExpenseBill.objects.get().billmunshiName, "MV-1")

    def test_the_source_is_removed_so_the_bill_is_not_in_two_places(self):
        bill, _ = self._vendor_bill()

        self._move("vendor", "expense", [bill.id])

        self.assertFalse(VendorBill.objects.filter(id=bill.id).exists())
        self.assertFalse(VendorZohoBill.objects.exists())
        self.assertEqual(ExpenseBill.objects.count(), 1)

    def test_the_move_is_recorded_with_the_old_id(self):
        from apps.common.models import BillActivity

        bill, _ = self._vendor_bill()
        self._move("vendor", "expense", [bill.id])

        entry = BillActivity.objects.filter(
            action=BillActivity.Action.MOVED,
        ).latest("created_at")
        self.assertEqual(entry.metadata["previous_bill_id"], str(bill.id))
        self.assertEqual(entry.metadata["to"], "Expense")

    # -- the destinations that were unreachable -----------------------

    def test_a_bill_can_be_moved_to_journal(self):
        """Supported by the backend but never offered by the old UI."""
        bill, _ = self._vendor_bill()

        response = self._move("vendor", "journal", [bill.id])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(JournalBill.objects.count(), 1)

    def test_a_bill_can_be_moved_out_of_sales(self):
        """A purchase bill uploaded to Sales used to be stuck there."""
        bill = SalesBill.objects.create(
            organization=self.org, billmunshiName="SL-1", status="Draft",
        )

        response = self._move("sales", "vendor", [bill.id])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(VendorBill.objects.count(), 1)

    def test_moving_to_payment_reports_the_lines_it_cannot_carry(self):
        """A payment voucher holds no line items, so saying so matters."""
        bill, _ = self._vendor_bill()

        response = self._move("vendor", "payment", [bill.id])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["moved_bills"][0]["line_items_dropped"], 1)
        self.assertIn("no line items", response.data["warning"])
        self.assertEqual(PaymentBill.objects.count(), 1)

    def test_a_cross_side_move_reports_the_party_it_cannot_carry(self):
        """A vendor is not a customer; the mapping cannot follow."""
        bill, _ = self._vendor_bill()

        response = self._move("vendor", "sales", [bill.id])

        self.assertEqual(response.data["moved_bills"][0]["party_cleared"], "vendor")

    # -- refusing rather than half-moving -----------------------------

    def test_a_synced_bill_is_refused(self):
        """Its voucher is already in Zoho; moving it would orphan that."""
        bill, _ = self._vendor_bill()
        VendorBill.objects.filter(id=bill.id).update(status="Synced")

        response = self._move("vendor", "expense", [bill.id])

        self.assertEqual(response.status_code, 400)
        self.assertIn("Synced", response.data["errors"][0]["error"])
        self.assertTrue(VendorBill.objects.filter(id=bill.id).exists())

    def test_a_bill_being_analysed_is_refused(self):
        """The worker would write its result into a row that is gone."""
        bill, _ = self._vendor_bill(is_processing=True)

        response = self._move("vendor", "expense", [bill.id])

        self.assertEqual(response.status_code, 400)
        self.assertIn("analysed right now", response.data["errors"][0]["error"])
        self.assertTrue(VendorBill.objects.filter(id=bill.id).exists())

    def test_a_failure_while_copying_lines_leaves_the_bill_where_it_was(self):
        """The old code logged this and deleted the source anyway."""
        bill, _ = self._vendor_bill()

        with patch(
            "apps.module.zoho.models.ExpenseZohoProduct.objects.create",
            side_effect=RuntimeError("boom"),
        ):
            response = self._move("vendor", "expense", [bill.id])

        self.assertEqual(response.status_code, 400)
        self.assertTrue(VendorBill.objects.filter(id=bill.id).exists())
        self.assertTrue(VendorZohoProduct.objects.exists())
        self.assertEqual(ExpenseBill.objects.count(), 0)

    def test_one_bad_bill_does_not_hold_back_the_good_ones(self):
        good, _ = self._vendor_bill()
        bad, _ = self._vendor_bill(name="MV-2")
        VendorBill.objects.filter(id=bad.id).update(status="Synced")

        response = self._move("vendor", "expense", [good.id, bad.id])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["successful_moves"], 1)
        self.assertEqual(response.data["failed_moves"], 1)

    # -- request validation -------------------------------------------

    def test_an_unknown_module_is_rejected_by_name(self):
        response = self._move("vendor", "nowhere", [])

        self.assertEqual(response.status_code, 400)
        self.assertIn("journal", response.data["detail"])

    def test_moving_to_the_same_module_is_rejected(self):
        response = self._move("vendor", "vendor", [])

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"], "Same Module")

    def test_an_empty_selection_is_rejected(self):
        response = self._move("vendor", "expense", [])

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"], "Missing Bill IDs")

    def test_a_bill_from_another_workspace_is_not_found(self):
        other_user = User.objects.create_user(
            email="o@example.com", password="x", first_name="O", last_name="X",
        )
        other = Organization.objects.create(
            name="Other", created_by=other_user, owner=other_user,
        )
        theirs = VendorBill.objects.create(
            organization=other, billmunshiName="TH-1", status="Draft",
        )

        response = self._move("vendor", "expense", [theirs.id])

        self.assertEqual(response.status_code, 400)
        self.assertTrue(VendorBill.objects.filter(id=theirs.id).exists())

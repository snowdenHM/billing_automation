"""MRP-priced lines, and the fields verify used to throw away.

Three separate holes, all of which made the verify screen a display
rather than a record of what the operator decided:

* line tax was recomputed as ``amount x rate`` on save, so both
  tax-base toggles were ornamental — the figure moved on screen and
  came back unchanged;
* a bill whose line amounts already hold their GST (an MRP-priced
  retail or pharmacy bill) had tax added on top of tax, and the
  arithmetic check then reported the bill as wrong by exactly its own
  tax;
* HSN, unit, place of supply, IRN and Ack No. were extracted and
  stored, and neither returned nor accepted back.
"""

from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.common.services.extraction.checks import run_checks
from apps.common.services.line_gst import assign_line_gst
from apps.module.tally.models import (
    TallyVendorAnalyzedBill, TallyVendorAnalyzedProduct, TallyVendorBill,
)
from apps.module.tally.views.vendor_bills import vendor_bill_verify
from apps.organizations.models import Organization
from apps.users.models import User


class InclusiveLineGstTests(TestCase):
    """``amount`` already holds the tax, so the rate applies in reverse."""

    def test_an_inclusive_line_is_taxed_out_of_its_amount(self):
        lines = [{"amount": 397.05, "rate": 5}]

        results, strategy = assign_line_gst(
            lines, 0, 9.46, 9.46, lines_include_gst=True,
        )

        # 397.05 x 5 / 105 = 18.91, leaving 378.14 taxable.
        self.assertEqual(
            results[0]["cgst"] + results[0]["sgst"], Decimal("18.91"),
        )
        self.assertEqual(results[0]["taxable_value"], Decimal("378.14"))
        self.assertEqual(strategy, "ocr-rates")

    def test_the_same_line_read_exclusively_is_taxed_on_top(self):
        """The contrast is the whole point of the flag."""
        lines = [{"amount": 397.05, "rate": 5}]

        results, _ = assign_line_gst(lines, 0, 9.93, 9.93)

        self.assertEqual(
            results[0]["cgst"] + results[0]["sgst"], Decimal("19.85"),
        )
        self.assertEqual(results[0]["taxable_value"], Decimal("397.05"))

    def test_an_unknown_rate_is_solved_the_inclusive_way(self):
        """The rate search has to apply the rate as the bill does."""
        lines = [{"amount": 1050.0, "rate": None}]

        results, strategy = assign_line_gst(
            lines, 0, 25.0, 25.0, lines_include_gst=True,
        )

        self.assertEqual(results[0]["rate"], 5.0)
        self.assertEqual(results[0]["taxable_value"], Decimal("1000.00"))
        self.assertEqual(strategy, "single-rate")

    def test_a_pre_tax_discount_still_applies_to_inclusive_lines(self):
        """Both readings can be true of the same bill."""
        lines = [{"amount": 1050.0, "rate": 5}]

        results, _ = assign_line_gst(
            lines, 0, 25.0, 25.0, taxable_base=525.0, lines_include_gst=True,
        )

        # Half the base, so half the tax: 525 x 5 / 105 = 25.00.
        self.assertEqual(
            results[0]["cgst"] + results[0]["sgst"], Decimal("25.00"),
        )
        self.assertEqual(results[0]["taxable_value"], Decimal("500.00"))

    def test_an_ordinary_bill_is_untouched_by_the_new_argument(self):
        lines = [{"amount": 10000.0, "rate": 18}]

        default, _ = assign_line_gst(lines, 1800.0, 0, 0)
        explicit, _ = assign_line_gst(
            lines, 1800.0, 0, 0, lines_include_gst=False,
        )

        self.assertEqual(default, explicit)
        self.assertEqual(default[0]["igst"], Decimal("1800.00"))


class ExtractionTotalCheckTests(TestCase):
    """The arithmetic check has to use the bill's own arithmetic."""

    def _codes(self, data):
        return {f["code"] for f in run_checks(data)}

    def test_a_discounted_bill_is_not_reported_as_a_mismatch(self):
        """The discount was simply left out of the sum."""
        data = {
            "items": [{"amount": 1000.0, "price": 1000.0, "quantity": 1}],
            "igst": 180.0, "discount": -100.0, "total": 1080.0,
        }

        self.assertNotIn("total_mismatch", self._codes(data))

    def test_an_mrp_bill_is_not_reported_as_wrong_by_its_own_tax(self):
        data = {
            "items": [{"amount": 397.05, "price": 397.05, "quantity": 1}],
            "cgst": 9.46, "sgst": 9.46, "total": 397.05,
            "lines_include_gst": True,
        }

        self.assertNotIn("total_mismatch", self._codes(data))

    def test_a_bill_that_really_does_not_add_up_is_still_reported(self):
        """The check still has to earn its place."""
        data = {
            "items": [{"amount": 1000.0, "price": 1000.0, "quantity": 1}],
            "igst": 180.0, "total": 5000.0,
        }

        self.assertIn("total_mismatch", self._codes(data))


class VerifyKeepsWhatTheScreenShowedTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            email="incl@example.com", password="x", first_name="I", last_name="N",
            is_staff=True, is_superuser=True,
        )
        cls.org = Organization.objects.create(
            name="Incl Co", created_by=cls.user, owner=cls.user,
        )

    def _bill(self, **kwargs):
        bill = TallyVendorBill.objects.create(
            organization=self.org, bill_munshi_name=f"TI-{kwargs.pop('_n', 1)}",
            status=TallyVendorBill.BillStatus.ANALYSED,
        )
        kwargs.setdefault("cgst", Decimal("900.00"))
        kwargs.setdefault("sgst", Decimal("900.00"))
        kwargs.setdefault(
            "gst_type", TallyVendorAnalyzedBill.GSTType.CGST_SGST,
        )
        analysed = TallyVendorAnalyzedBill.objects.create(
            organization=self.org, selected_bill=bill,
            total=Decimal("11800.00"), **kwargs,
        )
        product = TallyVendorAnalyzedProduct.objects.create(
            organization=self.org, vendor_bill_analyzed=analysed,
            item_name="Widget", price=Decimal("10000.00"), quantity=1,
            amount=Decimal("10000.00"), product_gst="18%",
            hsn="3004", unit="Nos",
        )
        return bill, analysed, product

    def _verify(self, bill, analyzed_data):
        request = APIRequestFactory().post(
            "/verify/",
            {"bill_id": str(bill.id), "analyzed_data": analyzed_data},
            format="json",
        )
        force_authenticate(request, user=self.user)
        return vendor_bill_verify(request, str(self.org.id))

    def _line(self, product, **over):
        line = {
            "item_id": str(product.id),
            "item_name": product.item_name,
            "price": "10000.00",
            "quantity": 1,
            "amount": "10000.00",
            "product_gst": "18%",
            "cgst": 810.0,
            "sgst": 810.0,
            "igst": 0.0,
        }
        line.update(over)
        return line

    def test_the_line_tax_the_screen_showed_is_what_gets_stored(self):
        """A pre-tax discount lowers it; recomputing threw that away."""
        bill, _, product = self._bill(discount_before_tax=True,
                                      discount=Decimal("-1000.00"))

        self._verify(bill, {"total_amount": 10620,
                            "products": [self._line(product)]})

        product.refresh_from_db()
        # 9000 x 18% split two ways, not 10000 x 18%.
        self.assertEqual(product.cgst, Decimal("810.00"))
        self.assertEqual(product.sgst, Decimal("810.00"))

    def test_the_heads_stay_a_server_decision(self):
        """A stale client must not post CGST+SGST on an inter-state bill."""
        # ``update_analyzed_bill_data`` re-derives gst_type from the
        # bill-level figures, so the bill has to really be inter-state
        # rather than just carry the label.
        bill, analysed, product = self._bill(
            _n=2, igst=Decimal("1800.00"),
            cgst=Decimal("0.00"), sgst=Decimal("0.00"),
            gst_type=TallyVendorAnalyzedBill.GSTType.IGST,
        )

        self._verify(bill, {
            "total_amount": 11620,
            "taxes": {"igst": {"amount": 1620.0},
                      "cgst": {"amount": 0.0},
                      "sgst": {"amount": 0.0}},
            "products": [self._line(product)],
        })

        product.refresh_from_db()
        self.assertEqual(product.igst, Decimal("1620.00"))
        self.assertEqual(product.cgst, Decimal("0.00"))
        self.assertEqual(product.sgst, Decimal("0.00"))

    def test_a_payload_without_line_tax_falls_back_to_amount_times_rate(self):
        """The older payload shape keeps working."""
        bill, _, product = self._bill(_n=3)
        line = self._line(product)
        for key in ("cgst", "sgst", "igst"):
            line.pop(key)

        self._verify(bill, {"total_amount": 11800, "products": [line]})

        product.refresh_from_db()
        self.assertEqual(product.cgst, Decimal("900.00"))
        self.assertEqual(product.sgst, Decimal("900.00"))

    def test_hsn_and_unit_survive_a_verify(self):
        """They were never sent, so a re-save used to blank them."""
        bill, _, product = self._bill(_n=4)

        self._verify(bill, {"total_amount": 11800,
                            "products": [self._line(product, hsn="3004",
                                                    unit="Nos")]})

        product.refresh_from_db()
        self.assertEqual(product.hsn, "3004")
        self.assertEqual(product.unit, "Nos")

    def test_hsn_can_be_corrected(self):
        bill, _, product = self._bill(_n=5)

        self._verify(bill, {"total_amount": 11800,
                            "products": [self._line(product, hsn="9983")]})

        product.refresh_from_db()
        self.assertEqual(product.hsn, "9983")

    def test_place_of_supply_irn_and_ack_are_saved(self):
        bill, analysed, product = self._bill(_n=6)

        self._verify(bill, {
            "total_amount": 11800,
            "place_of_supply": "27-Maharashtra",
            "irn": "a" * 64,
            "ack_no": "112410000123456",
            "products": [self._line(product)],
        })

        analysed.refresh_from_db()
        self.assertEqual(analysed.place_of_supply, "27-Maharashtra")
        self.assertEqual(analysed.irn, "a" * 64)
        self.assertEqual(analysed.ack_no, "112410000123456")

    def test_the_inclusive_flag_is_saved_and_can_be_turned_off(self):
        bill, analysed, product = self._bill(_n=7)

        self._verify(bill, {"total_amount": 11800, "lines_include_gst": True,
                            "products": [self._line(product)]})
        analysed.refresh_from_db()
        self.assertTrue(analysed.lines_include_gst)

        self._verify(bill, {"total_amount": 11800, "lines_include_gst": False,
                            "products": [self._line(product)]})
        analysed.refresh_from_db()
        self.assertFalse(analysed.lines_include_gst)

    def test_an_odd_paise_total_is_not_rounded_into_an_extra_paisa(self):
        """A total of 18.91 must come back as 18.91, not 18.92.

        Rounding each half of an odd-paise total independently gives
        9.46 twice. The paisa is invented, and it is invented on every
        such line, so a bill drifts from its own printed tax.
        """
        bill, _, product = self._bill(_n=9)

        self._verify(bill, {
            "total_amount": 11800,
            "products": [self._line(product, cgst=9.45, sgst=9.46)],
        })

        product.refresh_from_db()
        self.assertEqual(product.cgst + product.sgst, Decimal("18.91"))

    def test_the_fallback_path_splits_odd_paise_the_same_way(self):
        """The two splitting rules in the module are now one.

        A payload with no per-line tax falls back to amount x rate,
        which used to round each half on its own — so the same bill
        came back a paisa heavier through one path than the other.
        378.14 at 5% is 18.907, i.e. 18.91.
        """
        bill, _, product = self._bill(_n=10)
        product.amount = Decimal("378.14")
        product.product_gst = "5%"
        product.save(update_fields=["amount", "product_gst"])

        line = self._line(product, amount="378.14", product_gst="5%")
        for key in ("cgst", "sgst", "igst"):
            line.pop(key)
        self._verify(bill, {"total_amount": 397.05, "products": [line]})

        product.refresh_from_db()
        self.assertEqual(product.cgst + product.sgst, Decimal("18.91"))

    def test_reverse_charge_is_saved(self):
        bill, analysed, product = self._bill(_n=8)

        self._verify(bill, {"total_amount": 11800, "is_reverse_charge": True,
                            "products": [self._line(product)]})

        analysed.refresh_from_db()
        self.assertTrue(analysed.is_reverse_charge)


class LineDiscountTests(TestCase):
    """A discount against one line, and what reaches Tally for it."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            email="ld@example.com", password="x", first_name="L", last_name="D",
        )
        cls.org = Organization.objects.create(
            name="Line Disc Co", created_by=cls.user, owner=cls.user,
        )

    def _bill(self, discount, n=1):
        from apps.module.tally.models import Ledger, ParentLedger, TallyConfig
        TallyConfig.objects.update_or_create(
            organization=self.org, defaults={"tally_product_allow_sync": True},
        )
        parent = ParentLedger.objects.create(
            organization=self.org, parent=f"Purchase Accounts {n}",
        )
        ledger = Ledger.objects.create(
            organization=self.org, name=f"Purchase Accounts @5% {n}", parent=parent,
        )
        bill = TallyVendorBill.objects.create(
            organization=self.org, bill_munshi_name=f"LD-{n}",
        )
        analysed = TallyVendorAnalyzedBill.objects.create(
            organization=self.org, selected_bill=bill, bill_no=f"LD-{n}",
            total=Decimal("354.00"),
        )
        TallyVendorAnalyzedProduct.objects.create(
            organization=self.org, vendor_bill_analyzed=analysed,
            item_name="LIZOMED I V", price=Decimal("354.00"), quantity=1,
            amount=Decimal("354.00"), product_gst="5%", taxes=ledger,
            hsn="3004", unit="Nos", discount=discount,
        )
        return analysed

    def _item(self, analysed):
        from apps.module.tally.views.vendor_bills import prepare_sync_data
        return prepare_sync_data(analysed, self.org)["data"]["items"][0]

    def test_a_line_without_a_discount_is_emitted_untouched(self):
        """Every existing bill has to keep posting exactly as it did."""
        item = self._item(self._bill(Decimal("0.00"), n=1))

        self.assertEqual(item["price"], "354.00")
        self.assertEqual(item["amount"], "354.00")

    def test_a_line_discount_is_posted_net_with_no_new_tag(self):
        item = self._item(self._bill(Decimal("17.70"), n=2))

        self.assertEqual(item["amount"], "336.30")
        # Price is netted too, so price x quantity still equals amount —
        # a connector that re-derives the amount cannot drop the
        # discount without saying so.
        self.assertEqual(item["price"], "336.30")
        self.assertNotIn("discount", item)

    def test_the_sign_of_the_discount_does_not_change_the_answer(self):
        """It is a reduction either way; the sign is not a decision."""
        item = self._item(self._bill(Decimal("-17.70"), n=3))

        self.assertEqual(item["amount"], "336.30")

    def test_a_discount_larger_than_the_line_cannot_go_negative(self):
        item = self._item(self._bill(Decimal("9999.00"), n=4))

        self.assertEqual(item["amount"], "0.00")

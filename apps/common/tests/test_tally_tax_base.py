"""The pre-tax discount and taxable freight flags, end to end.

These two decide the value GST is charged on, so getting them wrong
misstates input credit: CGST s.15(3)(a) keeps an invoice-shown trade
discount out of taxable value, and s.15(2)(c) puts invoice freight
inside it.

The flags were on the model, in the serializer and set by the analysis
path — and the verify endpoint ignored them. So an operator who could
see the reading was wrong had no way to save a correction, which is the
same as the field not existing. That is what these cover.
"""

from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.module.tally.models import (
    TallyVendorAnalyzedBill, TallyVendorAnalyzedProduct, TallyVendorBill,
)
from apps.module.tally.views.vendor_bills import vendor_bill_verify
from apps.organizations.models import Organization
from apps.users.models import User


class VerifyAcceptsTaxBaseFlagsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            email="tb@example.com", password="x", first_name="T", last_name="B",
            is_staff=True, is_superuser=True,
        )
        cls.org = Organization.objects.create(
            name="Base Co", created_by=cls.user, owner=cls.user,
        )

    def _bill(self, **kwargs):
        bill = TallyVendorBill.objects.create(
            organization=self.org, bill_munshi_name="TB-1",
            status=TallyVendorBill.BillStatus.ANALYSED,
        )
        analysed = TallyVendorAnalyzedBill.objects.create(
            organization=self.org, selected_bill=bill,
            cgst=Decimal("900.00"), sgst=Decimal("900.00"),
            discount=Decimal("-1000.00"), freight=Decimal("500.00"),
            total=Decimal("10800.00"), **kwargs,
        )
        TallyVendorAnalyzedProduct.objects.create(
            organization=self.org, vendor_bill_analyzed=analysed,
            item_name="Widget", price=Decimal("10000.00"), quantity=1,
            amount=Decimal("10000.00"), product_gst="18%",
        )
        return bill, analysed

    def _verify(self, bill, payload_extra):
        request = APIRequestFactory().post(
            "/verify/",
            {
                "bill_id": str(bill.id),
                "analyzed_data": {"total_amount": 10800, **payload_extra},
            },
            format="json",
        )
        force_authenticate(request, user=self.user)
        return vendor_bill_verify(request, str(self.org.id))

    def test_verify_saves_a_pre_tax_discount_correction(self):
        """The operator read the bill; their correction has to stick."""
        bill, analysed = self._bill(discount_before_tax=False)

        self._verify(bill, {"discount_before_tax": True})

        analysed.refresh_from_db()
        self.assertTrue(analysed.discount_before_tax)

    def test_verify_saves_a_taxable_freight_correction(self):
        bill, analysed = self._bill(freight_taxable=False)

        self._verify(bill, {"freight_taxable": True})

        analysed.refresh_from_db()
        self.assertTrue(analysed.freight_taxable)

    def test_verify_can_turn_a_flag_back_off(self):
        """Extraction gets it wrong in both directions."""
        bill, analysed = self._bill(discount_before_tax=True)

        self._verify(bill, {"discount_before_tax": False})

        analysed.refresh_from_db()
        self.assertFalse(analysed.discount_before_tax)

    def test_a_payload_that_omits_the_flags_leaves_them_alone(self):
        """An older client must not silently reset them."""
        bill, analysed = self._bill(discount_before_tax=True, freight_taxable=True)

        self._verify(bill, {})

        analysed.refresh_from_db()
        self.assertTrue(analysed.discount_before_tax)
        self.assertTrue(analysed.freight_taxable)


class TaxableBaseTests(TestCase):
    """What the flags actually move: the value GST is charged on."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            email="tb2@example.com", password="x", first_name="T", last_name="C",
        )
        cls.org = Organization.objects.create(
            name="Base Co 2", created_by=cls.user, owner=cls.user,
        )

    def _analysed(self, **kwargs):
        bill = TallyVendorBill.objects.create(
            organization=self.org, bill_munshi_name=f"TB-{kwargs.get('_n', 1)}",
        )
        kwargs.pop("_n", None)
        analysed = TallyVendorAnalyzedBill.objects.create(
            organization=self.org, selected_bill=bill,
            discount=Decimal("-1000.00"), freight=Decimal("500.00"), **kwargs,
        )
        TallyVendorAnalyzedProduct.objects.create(
            organization=self.org, vendor_bill_analyzed=analysed,
            item_name="Widget", price=Decimal("10000.00"), quantity=1,
            amount=Decimal("10000.00"),
        )
        return analysed

    def test_neither_flag_leaves_the_base_to_the_lines(self):
        """None keeps assign_line_gst on its existing path untouched."""
        analysed = self._analysed(_n=1)

        self.assertIsNone(analysed.taxable_base)

    def test_a_pre_tax_discount_lowers_the_base(self):
        analysed = self._analysed(_n=2, discount_before_tax=True)

        self.assertEqual(analysed.taxable_base, 9000.0)

    def test_taxable_freight_raises_the_base(self):
        analysed = self._analysed(_n=3, freight_taxable=True)

        self.assertEqual(analysed.taxable_base, 10500.0)

    def test_both_together_net_off(self):
        analysed = self._analysed(
            _n=4, discount_before_tax=True, freight_taxable=True,
        )

        self.assertEqual(analysed.taxable_base, 9500.0)

    def test_the_base_never_goes_negative(self):
        """A mis-read discount larger than the bill must not invert it."""
        analysed = self._analysed(_n=5, discount_before_tax=True)
        analysed.discount = Decimal("-99999.00")

        self.assertEqual(analysed.taxable_base, 0.0)

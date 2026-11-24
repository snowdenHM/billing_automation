"""The GST heads on a bill can be corrected.

Client feedback behind correction 17: "bahut baar bill analysis meh
dekha hai ki CGST and SGST lagga hua hai toh uss case meh tax walle
section meh IGST show kar raha hai" — a bill that prints CGST and SGST
comes back showing a single IGST row.

Why it happened: analysis decides the heads from the figures it read::

    if igst_val > 0:      gst_type = IGST
    elif cgst or sgst:    gst_type = CGST_SGST

One stray value in the IGST column and the whole voucher is inter-state.
Worse, the split was treated as settled — ``line_tax_from_payload``
says it "stays a server decision from gst_type, so a stale client
cannot" change it — so the operator could see an intra-state invoice
rendered as an inter-state voucher and had no way to say so. The only
way out was to delete the bill and upload it again.

Verify now accepts a corrected head, validated against the model's own
choices. The amounts follow the heads on the voucher: one IGST becomes
an equal CGST and SGST.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.module.tally.models import (
    TallySalesAnalyzedBill, TallyVendorAnalyzedBill,
)
from apps.module.tally.views.vendor_bills import _split_gst_total
from apps.organizations.models import Organization

User = get_user_model()


class GstHeadCorrectionTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        owner = User.objects.create_user(
            email="gsthead@tally.test", password="x-test-pass-123",
        )
        cls.org = Organization.objects.create(
            name="GST Head Org", owner=owner, created_by=owner,
        )

    # -- the head is a real choice, not a derived constant --------------
    def test_both_vouchers_offer_both_heads(self):
        for model in (TallyVendorAnalyzedBill, TallySalesAnalyzedBill):
            with self.subTest(model=model.__name__):
                heads = {c for c, _ in model.GSTType.choices}
                self.assertIn("IGST", heads)
                self.assertIn("CGST_SGST", heads)

    def test_a_corrected_head_persists(self):
        bill = TallyVendorAnalyzedBill.objects.create(
            organization=self.org,
            gst_type=TallyVendorAnalyzedBill.GSTType.IGST,
        )
        bill.gst_type = TallyVendorAnalyzedBill.GSTType.CGST_SGST
        bill.save(update_fields=["gst_type"])
        bill.refresh_from_db()
        self.assertEqual(bill.gst_type, "CGST_SGST")

    # -- the figures follow the heads -----------------------------------
    def test_switching_to_intra_state_splits_the_tax_in_two(self):
        """180 of IGST is 90 CGST and 90 SGST, not 180 of each."""
        igst, cgst, sgst = _split_gst_total(
            180, TallyVendorAnalyzedBill.GSTType.CGST_SGST,
            TallyVendorAnalyzedBill,
        )
        self.assertEqual(igst, 0)
        self.assertEqual(cgst, 90)
        self.assertEqual(sgst, 90)
        self.assertEqual(cgst + sgst, 180, "the bill's tax changed amount")

    def test_switching_to_inter_state_puts_it_all_on_igst(self):
        igst, cgst, sgst = _split_gst_total(
            180, TallyVendorAnalyzedBill.GSTType.IGST,
            TallyVendorAnalyzedBill,
        )
        self.assertEqual(igst, 180)
        self.assertEqual(cgst, 0)
        self.assertEqual(sgst, 0)

    def test_the_total_tax_is_the_same_either_way(self):
        """Correcting the head must not move money, only its columns."""
        for total in (Decimal("180"), Decimal("2736.50"), Decimal("0.03")):
            with self.subTest(total=total):
                inter = _split_gst_total(
                    total, TallyVendorAnalyzedBill.GSTType.IGST,
                    TallyVendorAnalyzedBill,
                )
                intra = _split_gst_total(
                    total, TallyVendorAnalyzedBill.GSTType.CGST_SGST,
                    TallyVendorAnalyzedBill,
                )
                self.assertAlmostEqual(sum(inter), sum(intra), places=2)
                self.assertAlmostEqual(sum(inter), total, places=2)

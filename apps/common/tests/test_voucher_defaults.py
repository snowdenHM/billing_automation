"""Consolidation off by default; inclusive-GST lines on every voucher.

Two client corrections that are both about what a voucher looks like
the first time it is opened.

**Consolidation.** "Keep Consolidated items off for all the vouchers by
default." Two things forced it on: the payment and receipt models
defaulted to True, and analysis flipped the flag for any bill with two
or more lines on all three journal vouchers. So the operator's first
sight of a document was an aggregate, and the lines the bill actually
carries were behind a toggle. The consolidated row is still built — the
toggle has to have something to turn on — it just is not turned on for
them.

**Lines include GST.** "Add Including GST tab for Receipt voucher and
Payment Voucher." Purchase and sales have carried ``lines_include_gst``
since migration 0046; an MRP-priced document can just as easily be
receipted or paid, and without the flag the operator had to restate
every line by hand.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.module.tally.models import (
    TallyExpenseAnalyzedBill, TallyPaymentAnalyzedBill,
    TallyReceiptAnalyzedBill, TallySalesAnalyzedBill,
    TallyVendorAnalyzedBill,
)
from apps.organizations.models import Organization

User = get_user_model()

ANALYSED_BILL_MODELS = (
    TallyVendorAnalyzedBill, TallySalesAnalyzedBill,
    TallyExpenseAnalyzedBill, TallyPaymentAnalyzedBill,
    TallyReceiptAnalyzedBill,
)

#: The vouchers that carry ``lines_include_gst``. The journal entry
#: does not; see the test below.
INCLUSIVE_GST_MODELS = (
    TallyVendorAnalyzedBill, TallySalesAnalyzedBill,
    TallyPaymentAnalyzedBill, TallyReceiptAnalyzedBill,
)


class VoucherDefaultsTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        owner = User.objects.create_user(
            email="defaults@tally.test", password="x-test-pass-123",
        )
        cls.org = Organization.objects.create(
            name="Defaults Org", owner=owner, created_by=owner,
        )

    # -- consolidation --------------------------------------------------
    def test_no_voucher_opens_consolidated(self):
        for model in ANALYSED_BILL_MODELS:
            with self.subTest(model=model.__name__):
                bill = model.objects.create(organization=self.org)
                self.assertFalse(
                    bill.consolidate,
                    f"{model.__name__} still opens consolidated",
                )

    def test_nothing_in_the_views_forces_consolidation_on(self):
        """The model default is only half of it.

        Analysis used to flip the flag after creating the consolidated
        row, which defeated any default the model declared. Asserted
        over the source because the behaviour is a line of code in a
        path that needs a full AI analysis to reach.
        """
        import pathlib

        # apps/common/tests/<this file> -> parents[2] is `apps`.
        views = pathlib.Path(__file__).resolve().parents[2] / "module/tally/views"
        files = sorted(views.glob("*.py"))
        self.assertGreater(
            len(files), 5,
            f"the scan found no view files under {views} — it is not "
            "actually checking anything",
        )
        offenders = []
        for path in files:
            for n, line in enumerate(path.read_text().splitlines(), start=1):
                if "consolidate = True" in line and not line.strip().startswith("#"):
                    offenders.append(f"{path.name}:{n}: {line.strip()}")
        self.assertEqual(
            offenders, [],
            "a view forces consolidation on:\n" + "\n".join(offenders),
        )

    # -- inclusive GST --------------------------------------------------
    def test_the_asked_for_vouchers_can_hold_inclusive_lines(self):
        """Purchase, sales, receipt and payment.

        The journal voucher does not carry the flag. The correction
        named the receipt and the payment, and adding a column plus a
        toggle to a voucher nobody asked about is a change to make
        deliberately rather than for symmetry — so it is recorded here
        instead of quietly done.
        """
        for model in INCLUSIVE_GST_MODELS:
            with self.subTest(model=model.__name__):
                bill = model.objects.create(organization=self.org)
                self.assertFalse(
                    bill.lines_include_gst,
                    f"{model.__name__} defaults to inclusive lines",
                )
                bill.lines_include_gst = True
                bill.save(update_fields=["lines_include_gst"])
                bill.refresh_from_db()
                self.assertTrue(bill.lines_include_gst)

    def test_the_journal_voucher_has_no_inclusive_flag(self):
        """Pinned so the gap is a decision, not a surprise."""
        self.assertFalse(
            hasattr(TallyExpenseAnalyzedBill(), "lines_include_gst"),
            "the journal voucher gained lines_include_gst — give it the "
            "toggle and the balance handling too, or the column is "
            "another setting nothing reads",
        )

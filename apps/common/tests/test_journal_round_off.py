"""Auto round-off must work on the receipt, expense and payment vouchers.

Reported by a client on 2026-11-18: "Auto round off is not working for
Receipt voucher, check for Sales voucher too". The screenshot showed
Round Off (auto) sitting at 0.00 beside a stated Difference of ₹0.03,
with no way to clear it and Verify refusing the voucher until it was.

Why it could never fire. These three balance by sides rather than
against a total, and the rule was::

    round_off = DEBIT total - CREDIT total

The party row on these vouchers is derived from the lines — the amount
received is computed as whatever balances them — so that residual is
zero by construction. The arithmetic was asking whether the lines agree
with a figure calculated from the lines.

What the voucher actually has to reconcile is the lines against the
total printed on the document, which is what the vendor and sales
vouchers have always computed. ``_journal_round_off`` now does that for
all three, moves the party row onto the printed figure and lets the
round off carry the remainder, so the voucher balances *and* agrees
with the bill.

The threshold is unchanged: beyond ₹1 the gap is an extraction error,
not rounding, and must stay visible.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.module.tally.models import (
    Ledger, ParentLedger, TallyConfig,
    TallyExpenseAnalyzedBill, TallyExpenseAnalyzedProduct,
    TallyPaymentAnalyzedBill, TallyPaymentAnalyzedProduct,
    TallyReceiptAnalyzedBill, TallyReceiptAnalyzedProduct,
)
from apps.organizations.models import Organization

User = get_user_model()


class JournalRoundOffTests(TestCase):
    """Driven from the client's own figures where they are known."""

    @classmethod
    def setUpTestData(cls):
        owner = User.objects.create_user(
            email="roundoff@tally.test", password="x-test-pass-123",
        )
        cls.org = Organization.objects.create(
            name="Round Off Org", owner=owner, created_by=owner,
        )
        indirect = ParentLedger.objects.create(
            organization=cls.org, parent="Indirect Expenses",
        )
        cls.round_off_ledger = Ledger.objects.create(
            organization=cls.org, name="Round Off", parent=indirect,
        )
        config = TallyConfig.objects.create(organization=cls.org)
        config.round_off_parents.set([indirect])

    # -- the shapes each voucher takes ---------------------------------
    SHAPES = {
        "receipt": (TallyReceiptAnalyzedBill, TallyReceiptAnalyzedProduct,
                    "receipt_bill", "customer_amount", "customer_debit_or_credit"),
        "expense": (TallyExpenseAnalyzedBill, TallyExpenseAnalyzedProduct,
                    "expense_bill", "vendor_amount", "vendor_debit_or_credit"),
        "payment": (TallyPaymentAnalyzedBill, TallyPaymentAnalyzedProduct,
                    "payment_bill", "vendor_amount", "vendor_debit_or_credit"),
    }

    def build(self, kind, *, lines, igst=Decimal("0"), total,
              line_side="credit", party_side="debit"):
        model, product_model, fk, amount_field, dc_field = self.SHAPES[kind]
        bill = model.objects.create(
            organization=self.org,
            total=total,
            igst=igst,
            igst_debit_or_credit=line_side,
        )
        for amount in lines:
            product_model.objects.create(
                organization=self.org, amount=amount,
                debit_or_credit=line_side, **{fk: bill},
            )
        setattr(bill, amount_field, Decimal("0"))
        setattr(bill, dc_field, party_side)
        bill.save()
        return bill, amount_field

    # -- the bug -------------------------------------------------------
    def test_a_three_paise_gap_is_rounded_off(self):
        """The client's case: lines come to 0.03 over what the bill says."""
        for kind in self.SHAPES:
            with self.subTest(voucher=kind):
                bill, amount_field = self.build(
                    kind,
                    lines=[Decimal("62816.13")],
                    igst=Decimal("11306.90"),
                    total=Decimal("74123.00"),
                )
                bill.compute_round_off()
                bill.refresh_from_db()
                self.assertEqual(
                    bill.round_off, Decimal("0.03"),
                    f"{kind}: the round off stayed blank beside a real gap",
                )
                self.assertEqual(
                    getattr(bill, amount_field), Decimal("74123.00"),
                    f"{kind}: the party row did not take the printed total",
                )

    def test_the_round_off_ledger_is_picked(self):
        """A round off with no ledger cannot be verified."""
        bill, _ = self.build(
            "receipt", lines=[Decimal("62816.13")],
            igst=Decimal("11306.90"), total=Decimal("74123.00"),
        )
        bill.compute_round_off()
        self.assertEqual(bill.round_off_taxes, self.round_off_ledger)

    def test_the_voucher_balances_afterwards(self):
        """The point of the exercise: Tally rejects an unbalanced voucher."""
        for kind in self.SHAPES:
            with self.subTest(voucher=kind):
                bill, amount_field = self.build(
                    kind,
                    lines=[Decimal("62816.13")],
                    igst=Decimal("11306.90"),
                    total=Decimal("74123.00"),
                )
                bill.compute_round_off()
                bill.refresh_from_db()

                debit = credit = Decimal("0")
                rows = [
                    (p.amount, p.debit_or_credit) for p in bill.products.all()
                ] + [
                    (bill.igst, bill.igst_debit_or_credit),
                    (getattr(bill, amount_field),
                     getattr(bill, self.SHAPES[kind][4])),
                    (bill.round_off, bill.round_off_debit_or_credit),
                ]
                for amount, side in rows:
                    if not amount:
                        continue
                    if side == "debit":
                        debit += amount
                    elif side == "credit":
                        credit += amount
                self.assertEqual(
                    debit, credit,
                    f"{kind}: Dr {debit} != Cr {credit} after rounding off",
                )

    def test_a_gap_the_other_way_is_rounded_off(self):
        """The bill printing MORE than the lines is just as ordinary."""
        bill, amount_field = self.build(
            "receipt", lines=[Decimal("62816.13")],
            igst=Decimal("11306.90"), total=Decimal("74123.10"),
        )
        bill.compute_round_off()
        bill.refresh_from_db()
        self.assertEqual(bill.round_off, Decimal("0.07"))
        self.assertEqual(getattr(bill, amount_field), Decimal("74123.10"))

    # -- the limits stay where they were -------------------------------
    def test_a_gap_over_a_rupee_is_left_alone(self):
        """A real mismatch must stay visible, not be papered over."""
        for kind in self.SHAPES:
            with self.subTest(voucher=kind):
                bill, _ = self.build(
                    kind, lines=[Decimal("1000.00")],
                    total=Decimal("1250.00"),
                )
                bill.compute_round_off()
                bill.refresh_from_db()
                self.assertEqual(
                    bill.round_off, Decimal("0"),
                    f"{kind}: a ₹250 mismatch was rounded away",
                )

    def test_an_exact_bill_rounds_off_nothing(self):
        bill, _ = self.build(
            "receipt", lines=[Decimal("1000.00")], total=Decimal("1000.00"),
        )
        bill.compute_round_off()
        bill.refresh_from_db()
        self.assertEqual(bill.round_off, Decimal("0"))

    def test_without_a_printed_total_the_old_rule_still_applies(self):
        """A voucher with no stated total must not start failing."""
        bill, _ = self.build(
            "receipt", lines=[Decimal("500.00")], total=Decimal("0"),
        )
        bill.compute_round_off()
        self.assertEqual(bill.round_off, Decimal("0"))


class SalesRoundOffTests(TestCase):
    """"Check for Sales voucher too" — the client asked, so it is checked.

    Sales and vendor were never part of the bug above: they have always
    measured the gap against the printed total, which is the formula the
    three journal vouchers have just been moved onto. This test exists
    so the answer to the question is a test result rather than a reading
    of the code.
    """

    @classmethod
    def setUpTestData(cls):
        owner = User.objects.create_user(
            email="salesround@tally.test", password="x-test-pass-123",
        )
        cls.org = Organization.objects.create(
            name="Sales Round Org", owner=owner, created_by=owner,
        )
        indirect = ParentLedger.objects.create(
            organization=cls.org, parent="Indirect Expenses",
        )
        cls.ledger = Ledger.objects.create(
            organization=cls.org, name="Round Off", parent=indirect,
        )
        config = TallyConfig.objects.create(organization=cls.org)
        config.round_off_parents.set([indirect])

    def build(self, lines, igst, total):
        from apps.module.tally.models import (
            TallySalesAnalyzedBill, TallySalesAnalyzedProduct,
        )
        bill = TallySalesAnalyzedBill.objects.create(
            organization=self.org, total=total, igst=igst,
        )
        for amount in lines:
            TallySalesAnalyzedProduct.objects.create(
                organization=self.org, sales_bill_analyzed=bill, amount=amount,
            )
        return bill

    def test_a_three_paise_gap_is_rounded_off(self):
        bill = self.build(
            [Decimal("62816.13")], Decimal("11306.90"), Decimal("74123.00"),
        )
        bill.compute_round_off()
        bill.refresh_from_db()
        self.assertEqual(bill.round_off, Decimal("-0.03"))
        self.assertEqual(bill.round_off_taxes, self.ledger)

    def test_a_gap_over_a_rupee_is_left_alone(self):
        bill = self.build(
            [Decimal("1000.00")], Decimal("0"), Decimal("1250.00"),
        )
        bill.compute_round_off()
        bill.refresh_from_db()
        self.assertEqual(bill.round_off, Decimal("0"))

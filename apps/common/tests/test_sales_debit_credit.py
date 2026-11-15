"""One row shape for all three Tally feeds — built, and held back.

Asked on 2026-11-15: "we are missing Dr. and Cr. in Sales Voucher
response", then "journal ki tarah bilkul same chahiye". Built the same
day, then held back the same day: the Tally developer wants the sales
feed left exactly as it is for now.

So both flags are **off** and the payload is byte-for-byte what it has
always been. The behaviour is tested anyway, because the point of a
flag is that someone will flip it, and the day they do is the wrong day
to find out what it does.

What turning it on changes:

* every ``<ledger>`` gains ``<debit_or_credit>``, in the position the
  journal feed already puts it:
  ``amount, ledger, parent, [rate], debit_or_credit``
* no amount is ever negative. A row that reduces the bill — a discount,
  a downward round-off — goes out as a positive magnitude on the
  opposite side.

The side comes from the sign alone, against the voucher's direction: a
sale credits, a purchase debits, a reduction takes the opposite.
"""

from decimal import Decimal

from django.test import SimpleTestCase

from apps.module.tally.views import sales_bills as S, vendor_bills as V


def _forced(module, flag, amount):
    """``_dc_row`` with that feed's flag forced on, then restored."""
    before = module._dc_row.__globals__[flag]
    module._dc_row.__globals__[flag] = True
    try:
        return module._dc_row(amount)
    finally:
        module._dc_row.__globals__[flag] = before


def sale_row(amount):
    return _forced(S, "SALES_BILL_EMIT_DEBIT_CREDIT", amount)


def purchase_row(amount):
    return _forced(V, "VENDOR_BILL_EMIT_DEBIT_CREDIT", amount)


class ShippedStateTests(SimpleTestCase):
    """What actually goes out today."""

    def test_both_flags_are_off(self):
        """Held back on 2026-11-15 at the Tally developer's request.

        Both together — a payload where sales states a side and purchase
        does not is worse than one where neither does.
        """
        self.assertFalse(S.SALES_BILL_EMIT_DEBIT_CREDIT)
        self.assertFalse(V.VENDOR_BILL_EMIT_DEBIT_CREDIT)

    def test_nothing_is_added_to_the_payload(self):
        for feed in (S._dc_row, V._dc_row):
            value, extra = feed("-17.05")
            self.assertEqual(
                extra, {}, "a side reached a payload that must not carry one",
            )
            self.assertEqual(
                value, Decimal("-17.05"), "the stored sign was changed",
            )


class RowShapeTests(SimpleTestCase):
    """With the flag on: the journal's shape, both feeds."""

    def test_no_row_is_ever_negative(self):
        for feed in (sale_row, purchase_row):
            for amount in ("-17.05", "-0.35", Decimal("-1"), -99):
                value, _ = feed(amount)
                self.assertGreaterEqual(value, 0)

    def test_every_row_states_a_side(self):
        for feed in (sale_row, purchase_row):
            for amount in (0, 1, -1, "10000.00", "-17.05"):
                self.assertIn("debit_or_credit", feed(amount)[1])

    def test_junk_does_not_raise(self):
        for feed in (sale_row, purchase_row):
            for amount in (None, "", "abc", {}, []):
                value, extra = feed(amount)
                self.assertEqual(value, 0)
                self.assertIn("debit_or_credit", extra)


class SalesSidesTests(SimpleTestCase):
    """A sale credits what it is made of."""

    def side(self, amount):
        return sale_row(amount)[1]["debit_or_credit"]

    def test_revenue_and_output_tax_are_credited(self):
        self.assertEqual(self.side("10000.00"), "credit")
        self.assertEqual(self.side("900.00"), "credit")

    def test_a_discount_given_is_debited(self):
        amount, extra = sale_row("-17.05")
        self.assertEqual((amount, extra["debit_or_credit"]),
                         (Decimal("17.05"), "debit"))

    def test_round_off_follows_the_rounding(self):
        self.assertEqual(self.side("0.35"), "credit")
        self.assertEqual(self.side("-0.35"), "debit")


class PurchaseSidesTests(SimpleTestCase):
    """A purchase debits what it is made of — the mirror, not a copy."""

    def side(self, amount):
        return purchase_row(amount)[1]["debit_or_credit"]

    def test_the_cost_and_input_tax_are_debited(self):
        self.assertEqual(self.side("10000.00"), "debit")
        self.assertEqual(self.side("900.00"), "debit")

    def test_a_discount_received_is_credited(self):
        """The case an earlier version got backwards.

        It gave each row a "natural" side and then flipped it on a
        negative amount. A discount is always stored negative, so that
        flipped twice: -17.05 came out as 17.05 DEBIT, adding to the
        cost instead of reducing it.
        """
        amount, extra = purchase_row("-17.05")
        self.assertEqual((amount, extra["debit_or_credit"]),
                         (Decimal("17.05"), "credit"))

    def test_round_off_follows_the_rounding(self):
        self.assertEqual(self.side("0.35"), "debit")
        self.assertEqual(self.side("-0.35"), "credit")

    def test_the_two_feeds_are_opposites(self):
        for amount in ("10000.00", "900.00", "-17.05", "-0.35", "0.35"):
            self.assertNotEqual(
                sale_row(amount)[1]["debit_or_credit"],
                purchase_row(amount)[1]["debit_or_credit"],
                f"a sale and a purchase agreed about {amount}",
            )


class VoucherBalanceTests(SimpleTestCase):
    """A reduction must reduce what the party is left owing.

    The party is not a row — Tally posts it for whatever makes the
    voucher balance:

        purchase:  vendor credit   = sum(Dr) - sum(Cr)
        sale:      customer debit  = sum(Cr) - sum(Dr)

    and that has to come out at the bill's stated total. Reversing one
    row moves it by *twice* that row's amount, and nothing else in the
    payload looks wrong.
    """

    def implied(self, feed, amounts, party_side):
        rows = [feed(a) for a in amounts]
        debit = sum(v for v, e in rows if e["debit_or_credit"] == "debit")
        credit = sum(v for v, e in rows if e["debit_or_credit"] == "credit")
        return (debit - credit) if party_side == "credit" else (credit - debit)

    def test_a_purchase_reconciles_to_its_grand_total(self):
        """Lines 378.14 + tax 18.91 - discount 17.05 = 380.00 payable."""
        self.assertEqual(
            self.implied(purchase_row, ["378.14", "18.91", "-17.05"], "credit"),
            Decimal("380.00"),
        )

    def test_a_discount_reduces_what_the_vendor_is_owed(self):
        without = self.implied(purchase_row, ["378.14", "18.91"], "credit")
        with_ = self.implied(
            purchase_row, ["378.14", "18.91", "-17.05"], "credit")
        self.assertLess(with_, without, "the discount increased the bill")
        self.assertEqual(without - with_, Decimal("17.05"))

    def test_a_sale_reconciles_to_its_total(self):
        self.assertEqual(
            self.implied(sale_row, ["10000", "1800", "-17.05"], "debit"),
            Decimal("11782.95"),
        )

    def test_a_discount_reduces_what_the_customer_owes(self):
        without = self.implied(sale_row, ["10000", "1800"], "debit")
        with_ = self.implied(sale_row, ["10000", "1800", "-17.05"], "debit")
        self.assertLess(with_, without, "the discount increased the invoice")
        self.assertEqual(without - with_, Decimal("17.05"))

    def test_an_upward_round_off_increases_the_sale(self):
        without = self.implied(sale_row, ["11800"], "debit")
        with_ = self.implied(sale_row, ["11800", "0.33"], "debit")
        self.assertEqual(with_ - without, Decimal("0.33"))


class BalanceGuardTests(SimpleTestCase):
    """``_check_voucher_balance`` runs whatever the flag says."""

    GOOD = [
        {"amount": "378.14", "debit_or_credit": "debit"},
        {"amount": "18.91", "debit_or_credit": "debit"},
        {"amount": "17.05", "debit_or_credit": "credit"},
    ]

    def test_a_balanced_voucher_is_silent(self):
        with self.assertRaises(AssertionError):
            with self.assertLogs("apps.module.tally.views.vendor_bills",
                                 level="WARNING"):
                V._check_voucher_balance(self.GOOD, "380.00",
                                         party_side="credit",
                                         bill_ref="OK", dropped=[])

    def test_a_reversed_row_is_loud(self):
        reversed_discount = self.GOOD[:2] + [
            {"amount": "17.05", "debit_or_credit": "debit"},
        ]
        with self.assertLogs("apps.module.tally.views.vendor_bills",
                             level="WARNING") as logs:
            V._check_voucher_balance(reversed_discount, "380.00",
                                     party_side="credit",
                                     bill_ref="REVERSED", dropped=[])
        self.assertTrue(
            any("does not reconcile" in line for line in logs.output),
            logs.output,
        )

    def test_it_stays_quiet_when_rows_were_dropped(self):
        """The shortfall is already reported; do not report it twice."""
        with self.assertRaises(AssertionError):
            with self.assertLogs("apps.module.tally.views.vendor_bills",
                                 level="WARNING"):
                V._check_voucher_balance(
                    [{"amount": "1.00", "debit_or_credit": "debit"}],
                    "999.00", party_side="credit", bill_ref="X",
                    dropped=["500.00 has no purchase ledger"],
                )


class WireFormatTests(SimpleTestCase):
    """The ampersand, which is correct and keeps being questioned."""

    def xml(self):
        return str(V._sync_data_to_xml([{
            "id": "x", "bill_no": "1", "bill_date": "01-01-2026",
            "voucher_type": "Sales",
            "ledgers": [{"amount": "900.00", "ledger": "CGST (Output) @ 9%",
                         "parent": "Duties & Taxes", "rate": "18%",
                         "debit_or_credit": "credit"}],
        }]))

    def test_the_element_order_matches_the_journal_feed(self):
        xml = self.xml()
        at = [xml.find(f"<{t}>")
              for t in ("amount", "parent", "rate", "debit_or_credit")]
        self.assertNotIn(-1, at, "an element never reached the wire")
        self.assertEqual(at, sorted(at))

    def test_the_ampersand_is_escaped_exactly_once(self):
        """"Duties & Taxes" is Tally's own group name and must survive.

        Once is correct XML. Twice reaches Tally as the literal
        "&amp;"; not at all makes the document unparseable.
        """
        import xml.etree.ElementTree as ET

        raw = self.xml()
        self.assertIn("&amp;", raw)
        self.assertNotIn("&amp;amp;", raw)
        self.assertEqual(
            [p.text for p in ET.fromstring(raw).iter("parent")],
            ["Duties & Taxes"],
        )

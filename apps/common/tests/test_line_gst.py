"""Per-line GST must agree with the per-line rate, on every branch.

These are the cases that broke in production, plus the ones that must
keep working. The invariant under test throughout is

    line.tax == line.amount * line.rate / 100

and, where the bill's own totals are self-consistent, that the line tax
adds back up to what the invoice printed.
"""

from decimal import Decimal

from django.test import SimpleTestCase

from apps.common.services.line_gst import (
    assign_line_gst,
    line_rate_from_payload,
)


def totals(results):
    return (
        sum((r["igst"] for r in results), Decimal("0")),
        sum((r["cgst"] for r in results), Decimal("0")),
        sum((r["sgst"] for r in results), Decimal("0")),
    )


class AssignLineGstTests(SimpleTestCase):
    def assert_self_consistent(self, lines, results, inter_state=False):
        """Every line's tax is exactly its own amount times its own rate."""
        for line, result in zip(lines, results):
            expected = (
                Decimal(str(line["amount"])) * Decimal(str(result["rate"])) / 100
            ).quantize(Decimal("0.01"))
            got = result["igst"] if inter_state else result["cgst"] + result["sgst"]
            self.assertEqual(
                got, expected,
                f"line {line} claims {result['rate']}% but carries {got}",
            )

    def test_mixed_rate_bill_does_not_tax_the_exempt_line(self):
        """NSC/26-27/2340 — the bill that exposed this.

        Proportional splitting handed the 0%-rated POHA line ₹138.91 of
        CGST and left the taxable line short.
        """
        lines = [
            {"amount": 9047.60, "rate": None},   # Macroni, 5%
            {"amount": 14400.00, "rate": None},  # POHA, exempt
        ]
        results, strategy = assign_line_gst(lines, 0, 226.19, 226.19)

        self.assertEqual(strategy, "solved-mixed")
        self.assertEqual(results[0]["rate"], 5.0)
        self.assertEqual(results[1]["rate"], 0.0)
        self.assertEqual(results[0]["cgst"], Decimal("226.19"))
        self.assertEqual(results[1]["cgst"], Decimal("0.00"))
        self.assertEqual(totals(results), (Decimal("0.00"), Decimal("226.19"), Decimal("226.19")))
        self.assert_self_consistent(lines, results)

    def test_single_rate_bill(self):
        """NSC/26-27/1620 — one line, 5%, resolved without a search."""
        lines = [{"amount": 81190.00, "rate": None}]
        results, strategy = assign_line_gst(lines, 0, 2029.75, 2029.75)

        self.assertEqual(strategy, "single-rate")
        self.assertEqual(results[0]["rate"], 5.0)
        self.assertEqual(totals(results), (Decimal("0.00"), Decimal("2029.75"), Decimal("2029.75")))

    def test_inter_state_puts_everything_on_igst(self):
        lines = [{"amount": 48500.00, "rate": None}]
        results, _ = assign_line_gst(lines, 8730.00, 0, 0)

        self.assertEqual(results[0]["rate"], 18.0)
        self.assertEqual(results[0]["igst"], Decimal("8730.00"))
        self.assertEqual(results[0]["cgst"], Decimal("0.00"))
        self.assertEqual(results[0]["sgst"], Decimal("0.00"))
        self.assert_self_consistent(lines, results, inter_state=True)

    def test_three_way_mix(self):
        lines = [
            {"amount": 1000, "rate": None},
            {"amount": 2000, "rate": None},
            {"amount": 3000, "rate": None},
        ]
        results, strategy = assign_line_gst(lines, 0, 140.0, 140.0)

        self.assertEqual(strategy, "solved-mixed")
        self.assertEqual([r["rate"] for r in results], [18.0, 5.0, 0.0])
        self.assertEqual(totals(results), (Decimal("0.00"), Decimal("140.00"), Decimal("140.00")))
        self.assert_self_consistent(lines, results)

    def test_a_bill_with_no_tax_taxes_nothing(self):
        """An exempt bill must not have a rate inferred onto it."""
        lines = [{"amount": 5000, "rate": None}, {"amount": 7000, "rate": None}]
        results, strategy = assign_line_gst(lines, 0, 0, 0)

        self.assertEqual(strategy, "zero-rated")
        self.assertTrue(all(r["rate"] == 0.0 for r in results))
        self.assertEqual(totals(results), (Decimal("0.00"), Decimal("0.00"), Decimal("0.00")))

    def test_rates_read_from_the_bill_are_used_as_read(self):
        """When OCR got the per-line rate, nothing is inferred."""
        lines = [{"amount": 1000, "rate": 18}, {"amount": 2000, "rate": 5}]
        results, strategy = assign_line_gst(lines, 0, 140.0, 140.0)

        self.assertEqual(strategy, "ocr-rates")
        self.assertEqual([r["rate"] for r in results], [18.0, 5.0])
        self.assert_self_consistent(lines, results)

    def test_cgst_and_sgst_always_add_to_the_line_total(self):
        """An odd number of paise must not vanish in the halving."""
        lines = [{"amount": 333.33, "rate": 5}]
        results, _ = assign_line_gst(lines, 0, 8.33, 8.33)

        line = results[0]
        self.assertEqual(
            line["cgst"] + line["sgst"],
            (Decimal("333.33") * 5 / 100).quantize(Decimal("0.01")),
        )

    def test_unresolvable_bill_still_leaves_each_line_self_consistent(self):
        """The fallback may be approximate, but never self-contradictory.

        A bill whose printed tax matches no combination of slabs still
        has to produce rows whose rate explains their tax, because that
        is what the rollup and the voucher read.
        """
        lines = [{"amount": 1000, "rate": None}, {"amount": 1000, "rate": None}]
        # 7% blended: not a slab, and no slab pair reaches it either.
        results, strategy = assign_line_gst(lines, 0, 70.0, 70.0)

        self.assertEqual(strategy, "proportional-fallback")
        self.assert_self_consistent(lines, results)
        self.assertEqual(totals(results)[1], Decimal("70.00"))

    def test_no_lines(self):
        self.assertEqual(assign_line_gst([], 0, 10, 10), ([], "empty"))


class TaxableBaseTests(SimpleTestCase):
    """A pre-tax discount lowers the base; taxable freight raises it."""

    def test_pre_tax_discount_charges_gst_on_the_net(self):
        """CGST s.15(3)(a): a discount on the invoice leaves the base."""
        lines = [{"amount": 10000.0, "rate": None}]
        # 18% of 9,000 = 1,620, split 810/810.
        results, strategy = assign_line_gst(lines, 0, 810.0, 810.0, taxable_base=9000.0)

        self.assertEqual(strategy, "single-rate")
        self.assertEqual(results[0]["rate"], 18.0)
        self.assertEqual(results[0]["taxable_value"], Decimal("9000.00"))
        self.assertEqual(results[0]["cgst"], Decimal("810.00"))

    def test_taxable_freight_raises_the_base(self):
        """CGST s.15(2)(c): freight on the invoice is in the base."""
        lines = [{"amount": 10000.0, "rate": None}]
        # 18% of 10,500 = 1,890, split 945/945.
        results, strategy = assign_line_gst(lines, 0, 945.0, 945.0, taxable_base=10500.0)

        self.assertEqual(strategy, "single-rate")
        self.assertEqual(results[0]["rate"], 18.0)
        self.assertEqual(results[0]["taxable_value"], Decimal("10500.00"))

    def test_mixed_rates_still_solve_under_a_pre_tax_discount(self):
        """The hard case: both adjustments at once must still resolve."""
        lines = [
            {"amount": 9047.60, "rate": None},   # 5%
            {"amount": 14400.00, "rate": None},  # exempt
        ]
        # 10% pre-tax discount: base 21,102.84, tax 203.57 each side.
        results, strategy = assign_line_gst(
            lines, 0, 203.57, 203.57, taxable_base=21102.84
        )

        self.assertEqual(strategy, "solved-mixed")
        self.assertEqual([r["rate"] for r in results], [5.0, 0.0])
        self.assertEqual(results[1]["cgst"], Decimal("0.00"))
        self.assertEqual(totals(results)[1], Decimal("203.57"))

    def test_no_base_leaves_the_old_behaviour_alone(self):
        """Every existing bill must resolve exactly as it did before."""
        lines = [
            {"amount": 9047.60, "rate": None},
            {"amount": 14400.00, "rate": None},
        ]
        results, strategy = assign_line_gst(lines, 0, 226.19, 226.19)

        self.assertEqual(strategy, "solved-mixed")
        self.assertEqual([r["rate"] for r in results], [5.0, 0.0])
        for line, result in zip(lines, results):
            self.assertEqual(result["taxable_value"], Decimal(str(line["amount"])))


class HsnSummaryRateTests(SimpleTestCase):
    """The HSN summary table is the authoritative source for a rate."""

    SUMMARY = [
        {"hsn": "19021900", "cgst_rate": 2.5, "sgst_rate": 2.5},
        {"hsn": "11042900", "cgst_rate": 0, "sgst_rate": 0},
        {"hsn": "300490", "igst_rate": 12},
    ]

    def test_cgst_and_sgst_halves_add_to_the_rate(self):
        """2.5% + 2.5% is a 5% item, not a 2.5% one."""
        self.assertEqual(
            line_rate_from_payload({"hsn": "19021900"}, self.SUMMARY), 5.0
        )

    def test_igst_row_carries_the_full_rate(self):
        self.assertEqual(
            line_rate_from_payload({"hsn": "300490"}, self.SUMMARY), 12.0
        )

    def test_a_genuine_zero_rated_row_is_an_answer(self):
        self.assertEqual(
            line_rate_from_payload({"hsn": "11042900"}, self.SUMMARY), 0.0
        )

    def test_the_table_beats_the_line(self):
        """A crowded line column is read less reliably than the table."""
        self.assertEqual(
            line_rate_from_payload({"hsn": "19021900", "gst_rate": 18}, self.SUMMARY),
            5.0,
        )

    def test_falls_back_to_the_line_rate_without_an_hsn(self):
        self.assertEqual(
            line_rate_from_payload({"hsn": "", "gst_rate": 18}, self.SUMMARY), 18.0
        )

    def test_unknown_hsn_and_no_line_rate_is_unknown(self):
        """Unknown must stay None so the solver infers rather than guesses."""
        self.assertIsNone(line_rate_from_payload({"hsn": "99999999"}, self.SUMMARY))
        self.assertIsNone(line_rate_from_payload({}, self.SUMMARY))

    def test_an_extracted_rate_makes_the_solver_exact(self):
        """With rates read off the bill, nothing is inferred."""
        lines = [
            {"amount": 9047.60, "rate": line_rate_from_payload({"hsn": "19021900"}, self.SUMMARY)},
            {"amount": 14400.00, "rate": line_rate_from_payload({"hsn": "11042900"}, self.SUMMARY)},
        ]
        results, strategy = assign_line_gst(lines, 0, 226.19, 226.19)

        self.assertEqual(strategy, "ocr-rates")
        self.assertEqual(totals(results)[1], Decimal("226.19"))

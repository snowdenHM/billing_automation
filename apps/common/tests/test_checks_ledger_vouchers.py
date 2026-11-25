"""Checks must read a ledger voucher's lines, not only an item voucher's.

Purchase and sales put their lines under "items". Expense, payment and
receipt put theirs under "expenses". run_checks only looked at "items",
so every ledger voucher was told "No line items were found" however many
lines it carried, and the totals check — gated on the same list — never
ran on them at all.
"""
from django.test import SimpleTestCase

from apps.common.services.extraction.checks import run_checks


def codes(data):
    return {c["code"] for c in run_checks(data)}


BASE = {"invoiceNumber": "A-1", "dateIssued": "2026-04-01"}


class LedgerVoucherLinesTests(SimpleTestCase):
    def test_expenses_count_as_lines(self):
        """A receipt with expense lines is not an empty voucher."""
        data = {
            **BASE,
            "expenses": [
                {"description": "NEURACTIN-NF TAB", "category": "", "amount": 763.98},
                {"description": "GEMFUL 10 CAP", "category": "", "amount": 615.00},
            ],
            "total": 1378.98,
        }
        self.assertNotIn("no_items", codes(data))

    def test_empty_voucher_still_reports_no_items(self):
        """The finding is still right when there really are no lines."""
        self.assertIn("no_items", codes({**BASE, "total": 100}))
        self.assertIn("no_items", codes({**BASE, "expenses": [], "total": 100}))

    def test_items_still_win_when_both_present(self):
        """An item voucher keeps using its own key."""
        data = {
            **BASE,
            "items": [{"description": "x", "amount": 100, "price": 50, "quantity": 2}],
            "expenses": [{"description": "ignored", "amount": 999}],
            "total": 100,
        }
        self.assertNotIn("no_items", codes(data))
        self.assertNotIn("total_mismatch", codes(data))

    def test_totals_are_now_checked_on_ledger_vouchers(self):
        """The arithmetic that matters runs on them too.

        This is the half of the bug that was silent: with no lines to
        sum, total_mismatch could never fire on a payment or receipt,
        so a voucher whose lines did not add up to its total said
        nothing at all.
        """
        wrong = {
            **BASE,
            "expenses": [{"description": "a", "amount": 500.00}],
            "total": 1000.00,
        }
        self.assertIn("total_mismatch", codes(wrong))

        right = {
            **BASE,
            "expenses": [{"description": "a", "amount": 500.00}],
            "total": 500.00,
        }
        self.assertNotIn("total_mismatch", codes(right))

    def test_rate_times_quantity_is_skipped_without_those_keys(self):
        """An expense line has no price or quantity; that is not an error."""
        data = {
            **BASE,
            "expenses": [{"description": "Digital X-Ray", "category": "RA1",
                          "amount": 500.0}],
            "total": 500.0,
        }
        self.assertNotIn("line_arithmetic", codes(data))

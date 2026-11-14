"""A Zoho line must keep the quantity the document printed.

``create_vendor_zoho_objects_from_analysis`` read the quantity with
``int(...)`` and then computed ``amount = rate * quantity``. A "2.5 kg"
line was therefore stored as 2 and billed as ``2 x rate`` — the
fractional part left the bill, and the vendor was short-paid by it.

The Tally side has the same shape of bug and at least an excuse: its
``quantity`` column is a ``PositiveIntegerField``. Zoho's ``rate``,
``quantity`` and ``amount`` are all ``CharField``, so nothing forced the
truncation, and Zoho Books accepts a decimal quantity.

Also covered: the GST totals were read with a bare ``float()`` outside
any try, so a value the extraction wrote as "1,200.00" raised and took
the whole bill's product creation with it.
"""

from decimal import Decimal

from django.test import SimpleTestCase

from apps.common.converters import safe_float_convert, to_decimal


class ZohoLineAmountTests(SimpleTestCase):
    """What the converters must guarantee for a Zoho vendor line."""

    def line(self, price, quantity):
        """The arithmetic the view now performs."""
        rate = to_decimal(price, Decimal("0"))
        qty = to_decimal(quantity, Decimal("0"))
        if qty < 0:
            qty = Decimal("0")
        return rate, qty, rate * qty

    def test_a_fractional_quantity_keeps_its_money(self):
        rate, qty, amount = self.line(100, 2.5)
        self.assertEqual(qty, Decimal("2.50"), "the quantity was truncated")
        self.assertEqual(amount, Decimal("250.0000"), "the line lost money")

    def test_a_whole_quantity_is_unchanged(self):
        _, qty, amount = self.line(100, 3)
        self.assertEqual(qty, Decimal("3.00"))
        self.assertEqual(amount, Decimal("300.0000"))

    def test_a_negative_quantity_is_clamped(self):
        _, qty, amount = self.line(100, -3)
        self.assertEqual(qty, Decimal("0"))
        self.assertEqual(amount, Decimal("0"))

    def test_junk_does_not_raise(self):
        for price, quantity in ((None, None), ("abc", "xyz"), ("", ""),
                                ({}, []), ("1,200", "2")):
            rate, qty, amount = self.line(price, quantity)
            self.assertIsInstance(rate, Decimal)
            self.assertIsInstance(qty, Decimal)
            self.assertIsInstance(amount, Decimal)

    def test_gst_totals_survive_a_formatted_string(self):
        """`float("1,200.00")` raises; the view had no try around it."""
        for raw in ("1,200.00", None, "", "abc", {}, []):
            self.assertIsInstance(safe_float_convert(raw, 0.0), float)

    def test_gst_totals_still_read_a_real_number(self):
        self.assertEqual(safe_float_convert("1200.50", 0.0), 1200.50)
        self.assertEqual(safe_float_convert(1200.5, 0.0), 1200.5)


class ZohoViewWiringTests(SimpleTestCase):
    """The view must actually use the guarded conversions.

    Asserted against the source because the arithmetic above only proves
    the converters behave — not that the view calls them. The bug was
    never in the converters.
    """

    def source(self):
        import inspect

        from apps.module.zoho.views import vendor_bills

        return inspect.getsource(
            vendor_bills.create_vendor_zoho_objects_from_analysis
        )

    def test_the_quantity_is_not_int_cast(self):
        self.assertNotIn(
            "int(item.get('quantity'", self.source(),
            "the Zoho line quantity is being truncated again",
        )

    def test_the_gst_totals_are_guarded(self):
        src = self.source()
        for field in ("igst", "cgst", "sgst"):
            self.assertNotIn(
                f"float(relevant_data.get('{field}'", src,
                f"{field} is read with a bare float() again",
            )

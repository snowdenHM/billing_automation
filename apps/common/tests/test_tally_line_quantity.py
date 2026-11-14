"""A line's amount must not depend on which analysis path read it.

The quantity columns were ``PositiveIntegerField`` until migration 0058,
so "2.5 kg" was stored as 2. They are ``DecimalField`` now and the
quantity survives. The payload still emits ``int()`` until the Tally dev
confirms ``<quantity>`` takes a decimal — see
``TALLY_EMIT_DECIMAL_QUANTITY``.

The bug this file was written for is separate: the two paths which read
an extracted line disagreed about what to do with the rest of it:

* ``process_analysis_data`` (fresh analysis) multiplied the price by the
  *float* quantity, so a 2.5 x 100 line came to 250.
* ``process_existing_analysis_data`` (re-analysis) multiplied by the
  truncated *int*, so the same line came to 200.

Re-analysing a bill therefore moved money, by exactly the fractional part
of every quantity on it, with nothing on screen saying the total had
changed. Both paths now read the line through
``views.helpers.read_line_amounts``.
"""

from django.test import SimpleTestCase

from apps.module.tally.views.helpers import read_line_amounts


class ReadLineAmountsTests(SimpleTestCase):
    """What the helper promises: right money, truncated quantity, loudly."""

    def test_a_fractional_quantity_keeps_its_money(self):
        price, quantity, amount = read_line_amounts(
            {"price": 100, "quantity": 2.5, "description": "Rice 2.5kg"}
        )
        self.assertEqual(price, 100.0)
        self.assertEqual(amount, 250.0, "the fractional part was dropped from the amount")

    def test_a_fractional_quantity_survives_storage(self):
        """The column holds it now; nothing may round it on the way in."""
        from decimal import Decimal

        _, quantity, _ = read_line_amounts({"price": 100, "quantity": 2.5})
        self.assertEqual(quantity, Decimal("2.5"))
        self.assertIsInstance(quantity, Decimal)

    def test_three_decimal_places_are_kept(self):
        """Enough for kg / litre / metre, which is what this is for."""
        from decimal import Decimal

        _, quantity, _ = read_line_amounts({"price": 10, "quantity": "1.125"})
        self.assertEqual(quantity, Decimal("1.125"))

    def test_the_payload_still_emits_an_integer(self):
        """Until the Tally dev confirms, the sync bytes do not move."""
        from decimal import Decimal

        from apps.module.tally.views.vendor_bills import (
            TALLY_EMIT_DECIMAL_QUANTITY, _fmt_quantity,
        )

        self.assertFalse(
            TALLY_EMIT_DECIMAL_QUANTITY,
            "decimal quantities are being emitted into the Tally payload",
        )
        self.assertEqual(_fmt_quantity(Decimal("2.500")), 2)
        self.assertEqual(_fmt_quantity(Decimal("8.000")), 8)
        self.assertEqual(_fmt_quantity(None), 0)

    def test_both_analysis_paths_agree(self):
        """The two callers must compute one answer for one line.

        Written as a property over several shapes of line rather than one
        example, because the bug was not in any single number — it was in
        two places having their own copy of the arithmetic.
        """
        lines = [
            {"price": 100, "quantity": 2.5},
            {"price": 37.75, "quantity": 1.25},
            {"price": 1, "quantity": 0.5},
            {"price": 999.99, "quantity": 3},
            {"price": 0, "quantity": 10},
            {"price": 100, "quantity": 0},
        ]
        for line in lines:
            fresh = read_line_amounts(line, context="fresh analysis")
            again = read_line_amounts(line, context="re-analysis")
            self.assertEqual(
                fresh, again,
                f"the two paths disagree about {line}",
            )

    def test_missing_and_junk_values_do_not_raise(self):
        """Extraction output is not trusted to be numeric."""
        for line in ({}, {"price": None, "quantity": None},
                     {"price": "abc", "quantity": "xyz"},
                     {"price": "1,200.50", "quantity": "2"}):
            from decimal import Decimal

            price, quantity, amount = read_line_amounts(line)
            self.assertIsInstance(quantity, Decimal)
            self.assertGreaterEqual(quantity, 0)
            self.assertIsInstance(amount, float)

    def test_a_negative_quantity_does_not_reach_the_column(self):
        """A return line must not take the whole bill down with it.

        ``quantity`` carries a Postgres check constraint, so a negative
        value raises IntegrityError on save rather than storing badly —
        and an invoice line reading "LESS: RETURN 2 PCS" is ordinary
        enough to reach it. The quantity is clamped; the amount keeps its
        sign, because the money is the part that has to stay right.
        """
        from decimal import Decimal

        price, quantity, amount = read_line_amounts(
            {"price": 100, "quantity": -3, "description": "LESS: RETURN"}
        )
        self.assertEqual(quantity, Decimal("0"), "a negative quantity was stored")
        self.assertEqual(amount, -300.0, "the line lost the sign of its money")

    def test_the_clamped_sign_is_logged(self):
        with self.assertLogs("apps.module.tally.views.helpers", level="WARNING") as logs:
            read_line_amounts({"price": 100, "quantity": -3, "description": "R"})
        self.assertTrue(
            any("Negative quantity" in line for line in logs.output), logs.output,
        )

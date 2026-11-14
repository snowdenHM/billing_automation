"""A line's amount must not depend on which analysis path read it.

``TallyVendorAnalyzedProduct.quantity`` and its sales twin are
``PositiveIntegerField``, so "2.5 kg" is stored as 2. That is a known
limitation of the schema. What was not known is that the two paths which
read an extracted line disagreed about what to do with the rest of it:

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

    def test_a_fractional_quantity_is_truncated_for_storage(self):
        """The field cannot hold it; the caller must get the int it will store."""
        _, quantity, _ = read_line_amounts({"price": 100, "quantity": 2.5})
        self.assertEqual(quantity, 2)
        self.assertIsInstance(quantity, int)

    def test_the_truncation_is_logged(self):
        """A quantity that cannot be stored exactly must leave a trace."""
        with self.assertLogs("apps.module.tally.views.helpers", level="WARNING") as logs:
            read_line_amounts({"price": 100, "quantity": 2.5, "description": "Rice"})
        self.assertTrue(
            any("Fractional quantity" in line for line in logs.output),
            logs.output,
        )

    def test_a_whole_quantity_is_silent(self):
        """No warning for the ordinary case, or the log becomes noise."""
        import logging

        logger = logging.getLogger("apps.module.tally.views.helpers")
        with self.assertLogs(logger, level="WARNING") as logs:
            logger.warning("sentinel")  # assertLogs needs at least one record
            read_line_amounts({"price": 100, "quantity": 3})
        self.assertEqual(
            [line for line in logs.output if "Fractional" in line], [],
        )

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
            price, quantity, amount = read_line_amounts(line)
            self.assertIsInstance(quantity, int)
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
        price, quantity, amount = read_line_amounts(
            {"price": 100, "quantity": -3, "description": "LESS: RETURN"}
        )
        self.assertEqual(quantity, 0, "a negative quantity reached the column")
        self.assertEqual(amount, -300.0, "the line lost the sign of its money")

    def test_the_clamped_sign_is_logged(self):
        with self.assertLogs("apps.module.tally.views.helpers", level="WARNING") as logs:
            read_line_amounts({"price": 100, "quantity": -3, "description": "R"})
        self.assertTrue(
            any("Negative quantity" in line for line in logs.output), logs.output,
        )

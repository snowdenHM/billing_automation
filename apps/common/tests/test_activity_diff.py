"""Change sets: what counts as a correction and what is only reformatting.

These entries are the ground truth the extraction routes will be scored
against -- every operator correction is a free labelled example. An
entry that records no real change inflates that signal, so the line
between "the figure changed" and "the figure is written differently"
is worth pinning down.
"""

from django.test import SimpleTestCase

from apps.common.services.activity import diff_fields, same_value


class SameValueTests(SimpleTestCase):

    def test_formatting_only_differences_are_not_changes(self):
        for old, new in (
            ("0.00", "0"),
            ("0", "0.00"),
            ("1200", "1200.00"),
            ("1,200.00", "1200"),
            ("1,20,000.00", "120000"),
            ("  450.50  ", "450.50"),
            ("450.5", "450.50"),
            ("-0.00", "0"),
            ("", "   "),
            (" ABC Traders ", "ABC Traders"),
        ):
            self.assertTrue(same_value(old, new), f"{old!r} vs {new!r}")

    def test_real_differences_are_still_changes(self):
        for old, new in (
            ("0.00", "10.00"),
            ("0", "0.01"),
            ("1200", "1200.01"),
            ("", "0"),              # blank filled in is a correction
            ("0", ""),              # and cleared is too
            ("INV-1", "INV-2"),
            ("ACME", "Acme"),       # case matters in a name
            ("1200", "-1200"),
        ):
            self.assertFalse(same_value(old, new), f"{old!r} vs {new!r}")

    def test_non_numeric_lookalikes_fall_back_to_text(self):
        # Neither parses as a finite number, so neither shortcut applies.
        self.assertFalse(same_value("nan", "NaN"))
        self.assertFalse(same_value("Infinity", "inf"))


class DiffFieldsTests(SimpleTestCase):

    def test_a_reformatted_total_is_not_recorded(self):
        changes = diff_fields({"total": "0.00", "igst": "0.00"},
                              {"total": "0", "igst": "0"})
        self.assertEqual(changes, [])

    def test_a_real_edit_beside_a_reformat_records_only_the_edit(self):
        changes = diff_fields(
            {"total": "0.00", "igst": "0.00", "bill_no": "INV-1"},
            {"total": "10.00", "igst": "0", "bill_no": "INV-1"},
            labels={"total": "Total"},
        )
        self.assertEqual(changes, [
            {"field": "Total", "from": "0.00", "to": "10.00"},
        ])

    def test_the_raw_values_are_kept_not_the_normalised_ones(self):
        changes = diff_fields({"total": "1,200.00"}, {"total": "1500"})
        self.assertEqual(changes[0]["from"], "1,200.00")
        self.assertEqual(changes[0]["to"], "1500")

    def test_a_suggestion_the_user_merely_reformatted_counts_as_taken(self):
        changes = diff_fields(
            {"total": "90"}, {"total": "100.00"}, suggested={"total": "100"},
        )
        self.assertEqual(len(changes), 1)
        self.assertNotIn("suggested", changes[0])

    def test_a_suggestion_the_user_overrode_is_still_recorded(self):
        changes = diff_fields(
            {"total": "90"}, {"total": "100.00"}, suggested={"total": "105"},
        )
        self.assertEqual(changes[0]["suggested"], "105")

    def test_entries_are_sorted_by_field(self):
        changes = diff_fields({"b": "1", "a": "1"}, {"b": "2", "a": "2"})
        self.assertEqual([c["field"] for c in changes], ["a", "b"])

    def test_none_and_blank_are_the_same(self):
        self.assertEqual(diff_fields({"x": None}, {"x": ""}), [])

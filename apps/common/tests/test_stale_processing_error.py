"""A successful analysis must not leave the last failure on screen.

Reported by a client on 2026-11-15: a receipt voucher showed
"Analysis failed — AI analysis failed: FAILED", while its
``analysed_data`` was complete and correct and the status read
"Analysed".

``mark_processing_done`` clears ``processing_error`` and says in its own
docstring why. But only the background task goes through it. The
re-analyse endpoint calls ``process_<type>_analysis_data`` directly, so
a bill that failed once and was then analysed successfully kept the old
message for ever: the record showed its data beside an error that was
no longer true, and the list drew a red Error badge on a healthy row.

The clear now happens where the status is set to Analysed, which both
paths go through.
"""

import glob
import re

from django.test import SimpleTestCase


VIEWS = sorted(glob.glob("apps/module/tally/views/*_bills.py"))
SUCCESS = 'bill.process = True'
CLEAR = 'bill.processing_error = ""'


def success_blocks(path):
    """Each place a bill is marked analysed, with the lines after it."""
    lines = open(path, encoding="utf-8").read().split("\n")
    return [
        (i + 1, "\n".join(lines[i:i + 14]))
        for i, line in enumerate(lines)
        if line.strip() == SUCCESS
    ]


class StaleProcessingErrorTests(SimpleTestCase):

    def test_the_views_exist_to_be_checked(self):
        """A guard on the guard: an empty sweep must not look like a pass."""
        self.assertGreaterEqual(len(VIEWS), 5, VIEWS)
        self.assertTrue(all(success_blocks(p) for p in VIEWS))

    def test_every_success_clears_the_error(self):
        for path in VIEWS:
            for line_no, block in success_blocks(path):
                self.assertIn(
                    CLEAR, block,
                    f"{path}:{line_no} marks a bill analysed without clearing "
                    f"the previous run's processing_error — the record will "
                    f"show its data beside a failure that is no longer true.",
                )

    def test_every_success_persists_the_clear(self):
        """Clearing the attribute is not enough; it has to be saved."""
        for path in VIEWS:
            for line_no, block in success_blocks(path):
                saves = re.findall(r"update_fields=\[([^\]]*)\]", block)
                named = "processing_error" in block.split(CLEAR, 1)[1]
                self.assertTrue(
                    named or any("processing_error" in s for s in saves),
                    f"{path}:{line_no} clears processing_error but never "
                    f"writes it — update_fields leaves it out.",
                )

    def test_mark_processing_done_still_clears_it(self):
        """The background path's own clear must stay; both are needed."""
        import inspect

        from apps.common.services import tasks

        src = inspect.getsource(tasks.mark_processing_done)
        self.assertIn('processing_error = ""', src)
        self.assertIn("processing_error", src.split("_save_with_optional_stage")[1])

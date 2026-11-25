"""Sync must refuse a voucher the Tally feed would refuse.

Each voucher type has two payload builders, and they did not validate
the same things:

    POST <section>/sync/       -> get_structured_*_bill_data
    GET  <section>/sync_bills/ -> prepare_*_sync_data   (what Tally reads)

Only the second refuses an unbalanced voucher. So a payment or receipt
whose debits and credits disagreed was accepted by the first, marked
``Synced``, and then skipped by every feed the TCP bridge collected —
logged server-side, silent on screen. The voucher read as Synced for
ever and never reached Tally.

The endpoint now runs the feed\'s own builder first and returns its
refusal, before anything is marked Synced.

These read the view source rather than call the views: the decorators
wrap them, so ``inspect.getsource`` returns the DRF wrapper, and the
behaviour under test is an ordering property of the code itself.
"""
import pathlib
import re

from django.test import SimpleTestCase

# apps/common/tests/this.py -> parents[2] is apps/
VIEWS = pathlib.Path(__file__).resolve().parents[2] / "module" / "tally" / "views"

SECTIONS = [
    ("vendor_bills.py", "vendor_bill_sync", "prepare_sync_data"),
    ("sales_bills.py", "sales_bill_sync", "prepare_sync_data"),
    ("expense_bills.py", "expense_bill_sync", "prepare_expense_sync_data"),
    ("payment_bills.py", "payment_bill_sync", "prepare_payment_sync_data"),
    ("receipt_bills.py", "receipt_bill_sync", "prepare_receipt_sync_data"),
]


def body_of(filename, func):
    """The source of one function, decorators stripped."""
    src = (VIEWS / filename).read_text()
    start = src.index(f"def {func}(request")
    nxt = re.search(r"\n(?:@|def )", src[start:])
    return src[start:start + nxt.start()] if nxt else src[start:]


class SyncPreflightTests(SimpleTestCase):
    def test_every_section_preflights_with_the_feeds_builder(self):
        """All five, not just the two where it was noticed."""
        for filename, func, prepare in SECTIONS:
            body = body_of(filename, func)
            with self.subTest(func):
                self.assertIn(
                    f"{prepare}(analyzed_bill", body,
                    f"{func} never asks the builder the Tally feed uses",
                )
                self.assertIn("except ValueError", body,
                              f"{func} does not catch its refusal")
                self.assertIn("SYNC_PAYLOAD_INVALID", body,
                              f"{func} does not surface it to the operator")

    def test_refusal_comes_before_the_status_is_written(self):
        """Otherwise Synced is already stored when the feed later skips it."""
        for filename, func, prepare in SECTIONS:
            body = body_of(filename, func)
            with self.subTest(func):
                preflight = body.index(f"{prepare}(analyzed_bill")
                synced = body.index("BillStatus.SYNCED")
                self.assertLess(
                    preflight, synced,
                    f"{func} marks the bill Synced before validating the payload",
                )

    def test_refusal_is_a_422_not_a_500(self):
        """An unbalanced voucher is something to fix, not a crash."""
        for filename, func, prepare in SECTIONS:
            body = body_of(filename, func)
            with self.subTest(func):
                guard = body[body.index(f"{prepare}(analyzed_bill"):]
                guard = guard[:guard.index("BillStatus.SYNCED")]
                self.assertIn("HTTP_422_UNPROCESSABLE_ENTITY", guard)

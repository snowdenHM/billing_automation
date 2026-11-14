"""A reverse-charge sale that still charges GST must not sync.

Under s.9(3)/9(4) the recipient self-assesses the tax, so the supplier —
the client issuing the invoice — does not charge GST on it. An invoice
marked reverse charge that still carries output IGST/CGST/SGST is
stating two incompatible things.

The sales side stored ``is_reverse_charge`` and did nothing with it, so
such an invoice synced with its output tax intact and overstated the
client's liability on the return.

Which half is wrong — the flag or the tax — is not something code can
decide from here, so this refuses and says so rather than dropping the
tax quietly.
"""

from decimal import Decimal

from django.test import SimpleTestCase

from apps.module.tally.reverse_charge import sales_rcm_blockers


class _Bill:
    """The three fields the blocker reads."""

    def __init__(self, rcm=False, igst=0, cgst=0, sgst=0):
        self.is_reverse_charge = rcm
        self.igst, self.cgst, self.sgst = igst, cgst, sgst


class SalesRcmBlockerTests(SimpleTestCase):

    def test_an_ordinary_sale_is_never_blocked(self):
        self.assertEqual(sales_rcm_blockers(_Bill(rcm=False, igst=180)), [])

    def test_a_reverse_charge_sale_with_no_gst_is_fine(self):
        """This is what a correct reverse-charge invoice looks like."""
        self.assertEqual(sales_rcm_blockers(_Bill(rcm=True)), [])

    def test_a_reverse_charge_sale_charging_gst_is_blocked(self):
        reasons = sales_rcm_blockers(_Bill(rcm=True, igst=Decimal("180.00")))
        self.assertEqual(len(reasons), 1)
        self.assertIn("still charges output GST", reasons[0])

    def test_the_reason_names_the_taxes_actually_charged(self):
        """An operator has to know which figure to go and look at."""
        reasons = sales_rcm_blockers(
            _Bill(rcm=True, cgst=Decimal("90.00"), sgst=Decimal("90.00"))
        )
        self.assertIn("CGST 90.00", reasons[0])
        self.assertIn("SGST 90.00", reasons[0])
        self.assertNotIn("IGST", reasons[0])

    def test_the_reason_says_what_to_do(self):
        reasons = sales_rcm_blockers(_Bill(rcm=True, igst=Decimal("180.00")))
        self.assertIn("clear the GST", reasons[0])
        self.assertIn("turn reverse charge off", reasons[0])

    def test_zero_tax_fields_do_not_count_as_charged(self):
        bill = _Bill(rcm=True, igst=Decimal("0.00"),
                     cgst=Decimal("0"), sgst=None)
        self.assertEqual(sales_rcm_blockers(bill), [])

    def test_it_is_wired_into_the_sync_view(self):
        """A blocker nobody calls blocks nothing.

        Read off the module source, not ``inspect.getsource`` of the
        view: the DRF decorators replace it with a wrapper, so that
        only ever returns the wrapper's own body.
        """
        import inspect

        from apps.module.tally.views import sales_bills

        src = inspect.getsource(sales_bills)
        body = src.split("def sales_bill_sync(")[1].split("\ndef ")[0]
        self.assertIn("sales_rcm_blockers", body)
        self.assertIn("SALES_RCM_CONFLICT", body)

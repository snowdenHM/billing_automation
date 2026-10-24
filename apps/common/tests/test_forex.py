"""The forex conversion, and the promise it must not break.

The Tally TCP connector binds to absolute paths in the XML this backend
emits (``XML Object Path : "data:1"``), so a tag that moves, appears or
disappears fails *silently*: the GET returns 200, the bills show up in
Tally's request log, and no voucher is created. Converting amounts into
INR at the sync boundary is therefore allowed to change values and
nothing else, and these tests are the durable statement of that.
"""

import copy
from decimal import Decimal
from unittest import mock
from xml.etree import ElementTree as ET

from django.core.cache import cache
from django.test import SimpleTestCase

from apps.common.services import forex
from apps.module.tally.views.vendor_bills import _sync_data_to_xml


def _payload():
    """One realistic prepared sync-bill dict, as ``prepare_sync_data`` builds it."""
    return {
        "id": "de54c235-0174-4f83-9d87-a1db0cad9081",
        "bill_no": "A-004759",
        "bill_date": "16-05-2026",
        "voucher_type": "Purchase",
        "vendor_name": "AGGARWAL TRADE LINK",
        "vendor_gst_in": "27AABCU9603R1ZM",
        "vendor_parent": "Sundry Creditors",
        "company": "Local Dev",
        "total_amount": "3776.00",
        "notes": "Bill from AGGARWAL TRADE LINK entered via BillMunshi",
        "ledgers": [
            {"amount": "288.00", "ledger": "CGST (ITC) @ 9%",
             "parent": "Duties & Taxes", "rate": "18%"},
            {"amount": "288.00", "ledger": "SGST (ITC) @ 9%",
             "parent": "Duties & Taxes", "rate": "18%"},
        ],
        "items": [
            {"name": '1" SELF ADHESIVE TAPE', "details": "Trophies 9502 Medium",
             "purchase_ledger": "PURCHASE ACCOUNTS @18%",
             "price": "400.00", "quantity": "8", "amount": "3200.00"},
        ],
    }


def _skeleton(xml):
    """Every tag path in document order — what the connector binds to."""
    paths = []

    def walk(elem, prefix):
        path = f"{prefix}/{elem.tag}"
        paths.append(path)
        for child in elem:
            walk(child, path)

    walk(ET.fromstring(xml), "")
    return paths


class ConvertSyncPayloadTests(SimpleTestCase):
    def test_rate_one_is_byte_identical(self):
        """Every bill that existed before forex shipped has rate 1."""
        before = _payload()
        after = forex.convert_sync_payload_to_inr(copy.deepcopy(before), Decimal("1"))
        self.assertEqual(after, before)
        self.assertEqual(
            _sync_data_to_xml([after]), _sync_data_to_xml([before]),
            "rate 1.000000 must leave the payload bit-for-bit unchanged",
        )

    def test_conversion_never_changes_the_xml_shape(self):
        before = _payload()
        after = forex.convert_sync_payload_to_inr(_payload(), Decimal("83.20"))

        xml_before = _sync_data_to_xml([before])
        xml_after = _sync_data_to_xml([after])

        self.assertEqual(_skeleton(xml_before), _skeleton(xml_after))
        self.assertNotEqual(xml_before, xml_after, "values should have changed")
        self.assertEqual(list(before), list(after), "key order must not change")

    def test_money_is_scaled_exactly(self):
        after = forex.convert_sync_payload_to_inr(_payload(), Decimal("83.20"))
        self.assertEqual(after["total_amount"], "314163.20")
        self.assertEqual(after["ledgers"][0]["amount"], "23961.60")
        self.assertEqual(after["items"][0]["price"], "33280.00")
        self.assertEqual(after["items"][0]["amount"], "266240.00")

    def test_quantities_and_gst_rates_are_not_money(self):
        """Multiplying either by an exchange rate is silently, hugely wrong."""
        after = forex.convert_sync_payload_to_inr(_payload(), Decimal("83.20"))
        self.assertEqual(after["items"][0]["quantity"], "8")
        self.assertEqual(after["ledgers"][0]["rate"], "18%")
        self.assertEqual(after["bill_no"], "A-004759")
        self.assertEqual(after["bill_date"], "16-05-2026")

    def test_garbage_rate_is_a_no_op_rather_than_a_crash(self):
        before = _payload()
        self.assertEqual(forex.convert_sync_payload_to_inr(_payload(), "not a number"), before)
        self.assertEqual(forex.convert_sync_payload_to_inr(_payload(), None), before)


class ToInrTests(SimpleTestCase):
    def test_rupee_amount_is_returned_untouched(self):
        self.assertEqual(forex.to_inr("3776.00", Decimal("1")), Decimal("3776.00"))

    def test_conversion_rounds_to_paise(self):
        self.assertEqual(forex.to_inr("100", Decimal("83.204567")), Decimal("8320.46"))

    def test_non_numbers_are_none_not_zero(self):
        # Zero would post a voucher line of ₹0 and look deliberate.
        self.assertIsNone(forex.to_inr("abc", Decimal("83.2")))
        self.assertIsNone(forex.to_inr(None, Decimal("83.2")))


class RateFetchTests(SimpleTestCase):
    def setUp(self):
        cache.delete("forex:rates:inr")
        cache.delete("forex:rates:inr:last_good")

    tearDown = setUp

    def test_inr_never_touches_the_network(self):
        with mock.patch.object(forex.requests, "get", side_effect=AssertionError("called")):
            rate, meta = forex.rate_for("INR")
        self.assertEqual(rate, Decimal("1.000000"))
        self.assertEqual(meta["source"], "base")

    def test_outage_falls_back_to_the_last_good_rates(self):
        """An operator must still be able to work when the provider is down."""
        good = {
            "base": "INR",
            "rates": {"INR": "1.000000", "USD": "83.200000"},
            "fetched_at": "2026-03-01T00:00:00+00:00",
            "provider": "open.er-api.com",
        }
        cache.set("forex:rates:inr:last_good", good, 60)

        with mock.patch.object(forex.requests, "get", side_effect=OSError("down")):
            payload = forex.get_rates()

        self.assertEqual(payload["source"], "stale")
        self.assertTrue(payload["stale"])
        self.assertEqual(payload["rates"]["USD"], "83.200000")
        self.assertEqual(payload["fetched_at"], "2026-03-01T00:00:00+00:00")

    def test_outage_with_no_cache_at_all_raises(self):
        with mock.patch.object(forex.requests, "get", side_effect=OSError("down")):
            with self.assertRaises(forex.ForexUnavailable):
                forex.get_rates()

    def test_provider_quotes_are_inverted_into_inr_per_unit(self):
        """The provider quotes USD-per-INR; a bill needs INR-per-USD."""
        response = mock.Mock()
        response.raise_for_status = mock.Mock()
        response.json.return_value = {
            "result": "success",
            "time_last_update_unix": 1772236800,
            # 1 INR = 0.0120192 USD  ->  1 USD = 83.2001 INR
            "rates": {"USD": 0.0120192, "AED": 0.0441, "JPY": 1.7},
        }
        with mock.patch.object(forex.requests, "get", return_value=response):
            payload = forex.get_rates(force_refresh=True)

        self.assertEqual(payload["source"], "live")
        self.assertEqual(Decimal(payload["rates"]["USD"]).quantize(Decimal("0.01")),
                         Decimal("83.20"))
        # JPY quotes below one rupee; six decimal places must survive it.
        self.assertEqual(payload["rates"]["JPY"], "0.588235")
        # AED is why this provider was chosen over the ECB feed.
        self.assertIn("AED", payload["rates"])

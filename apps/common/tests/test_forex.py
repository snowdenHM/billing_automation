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


# ---------------------------------------------------------------------------
# CBIC notified rates
# ---------------------------------------------------------------------------
# The notified rate is the one a customs or GST-on-imports computation is
# allowed to use. These tests pin the three things that would silently
# misstate a return if they broke: the per-unit division, the import /
# export split, and the refusal to substitute a market rate when the
# notified one is missing.

from apps.common.services import cbic  # noqa: E402


def _cbic_body():
    """One real response, trimmed — captured from the live service."""
    return {
        "status": 1,
        "notStartDate": "16-10-2026",
        "notEndDate": "15-10-2026",
        "error": None,
        "currencyDetail": [
            {"currencyCode": "USD", "currencyDesc": "US Dollar",
             "cbicImport": 95.9, "cbicExport": 94.2, "units": "1.0"},
            {"currencyCode": "JPY", "currencyDesc": "Japanese Yen",
             "cbicImport": 61.2, "cbicExport": 59.35, "units": "100.0"},
            {"currencyCode": "AED", "currencyDesc": "UAE Dirham",
             "cbicImport": 26.65, "cbicExport": 25.15, "units": "1.0"},
        ],
    }


def _cbic_notifications():
    return [
        {"notificationNumber": "29/2026", "notPublishDate": "15-10-2026"},
        {"notificationNumber": "28/2026", "notPublishDate": "01-10-2026"},
    ]


def _mock_post(rates_body=None, notifications=None, rates_exc=None):
    """Stand in for both POSTs the module makes, routed by URL."""
    def _post(url, *args, **kwargs):
        response = mock.Mock()
        response.raise_for_status = mock.Mock()
        if url == cbic._RATES_URL:
            if rates_exc:
                raise rates_exc
            response.json.return_value = (
                _cbic_body() if rates_body is None else rates_body
            )
        else:
            response.json.return_value = (
                _cbic_notifications() if notifications is None else notifications
            )
        return response
    return _post


class CbicNotifiedRateTests(SimpleTestCase):
    def setUp(self):
        cache.delete(cbic._FRESH_KEY)
        cache.delete(cbic._STALE_KEY)

    tearDown = setUp

    def test_import_and_export_are_different_numbers(self):
        """The whole point of the split: reading the wrong column is a
        1.8% misstatement on USD, not a rounding difference."""
        with mock.patch.object(cbic.requests, "post", side_effect=_mock_post()):
            imp, _ = cbic.notified_rate_for("USD", cbic.SIDE_IMPORT)
            exp, meta = cbic.notified_rate_for("USD", cbic.SIDE_EXPORT)

        self.assertEqual(imp, Decimal("95.900000"))
        self.assertEqual(exp, Decimal("94.200000"))
        self.assertEqual(meta["side"], "export")

    def test_yen_is_divided_by_its_notified_unit(self):
        """CBIC notifies JPY per 100. Storing 61.20 as the rate for one
        yen would post a Japanese invoice at a hundred times its value."""
        with mock.patch.object(cbic.requests, "post", side_effect=_mock_post()):
            rate, _ = cbic.notified_rate_for("JPY", cbic.SIDE_IMPORT)
        self.assertEqual(rate, Decimal("0.612000"))

    def test_the_notification_number_travels_with_the_rate(self):
        """'We used the CBIC rate' is not an answer to an auditor."""
        with mock.patch.object(cbic.requests, "post", side_effect=_mock_post()):
            _, meta = cbic.notified_rate_for("AED", cbic.SIDE_IMPORT)

        self.assertEqual(meta["notification"], "29/2026")
        self.assertEqual(meta["effective_from"], "15-10-2026")
        self.assertEqual(cbic.reference_for(meta), "CBIC 29/2026 w.e.f. 15-10-2026")

    def test_a_side_must_be_named(self):
        """No default. Defaulting is the silent guess the split exists
        to prevent."""
        with self.assertRaises(ValueError):
            cbic.notified_rate_for("USD", "")
        with self.assertRaises(ValueError):
            cbic.notified_rate_for("USD", "buy")

    def test_outage_falls_back_to_the_last_notified_set(self):
        good = {
            "rates": {"USD": {"import": "95.900000", "export": "94.200000",
                              "units": "1.0", "label": "US Dollar"}},
            "effective_from": "15-10-2026",
            "notification": "29/2026",
            "fetched_at": "2026-10-15T18:30:00+00:00",
            "provider": cbic.PROVIDER_NAME,
        }
        cache.set(cbic._STALE_KEY, good, 60)

        with mock.patch.object(cbic.requests, "post",
                               side_effect=_mock_post(rates_exc=OSError("down"))):
            payload = cbic.get_notified_rates()

        self.assertEqual(payload["source"], "stale")
        self.assertTrue(payload["stale"])
        self.assertEqual(payload["rates"]["USD"]["import"], "95.900000")

    def test_outage_with_no_cache_raises_rather_than_guessing(self):
        with mock.patch.object(cbic.requests, "post",
                               side_effect=_mock_post(rates_exc=OSError("down"))):
            with self.assertRaises(cbic.CbicUnavailable):
                cbic.get_notified_rates()

    def test_a_missing_notification_list_does_not_lose_the_rates(self):
        """The citation is nice to have; the rate is the job."""
        def _post(url, *args, **kwargs):
            if url == cbic._NOTIFICATIONS_URL:
                raise OSError("down")
            response = mock.Mock()
            response.raise_for_status = mock.Mock()
            response.json.return_value = _cbic_body()
            return response

        with mock.patch.object(cbic.requests, "post", side_effect=_post):
            rate, meta = cbic.notified_rate_for("USD", cbic.SIDE_IMPORT)

        self.assertEqual(rate, Decimal("95.900000"))
        self.assertIsNone(meta["notification"])
        self.assertEqual(cbic.reference_for(meta), "CBIC notified w.e.f. 15-10-2026")

    def test_a_currency_cbic_does_not_notify_is_unavailable_not_zero(self):
        with mock.patch.object(cbic.requests, "post", side_effect=_mock_post()):
            with self.assertRaises(cbic.CbicUnavailable):
                cbic.notified_rate_for("SGD", cbic.SIDE_IMPORT)

    def test_garbage_from_the_service_is_an_outage_not_a_crash(self):
        """It is an undocumented endpoint. A changed shape must degrade
        to 'no notified rate', never to a 500 on a bill screen."""
        for body in ({"currencyDetail": []}, {"error": "boom"}, [], "nope"):
            with self.subTest(body=body):
                cache.delete(cbic._FRESH_KEY)
                cache.delete(cbic._STALE_KEY)
                with mock.patch.object(cbic.requests, "post",
                                       side_effect=_mock_post(rates_body=body)):
                    with self.assertRaises(cbic.CbicUnavailable):
                        cbic.get_notified_rates()


class CbicIsNotTheMarketRateTests(SimpleTestCase):
    """The two sources are separate and must stay separate."""

    def setUp(self):
        for key in (cbic._FRESH_KEY, cbic._STALE_KEY,
                    "forex:rates:inr", "forex:rates:inr:last_good"):
            cache.delete(key)

    tearDown = setUp

    def test_cbic_outage_does_not_touch_the_market_cache(self):
        market = {
            "base": "INR",
            "rates": {"INR": "1.000000", "USD": "88.400000"},
            "fetched_at": "2026-10-24T00:00:00+00:00",
            "provider": "open.er-api.com",
        }
        cache.set("forex:rates:inr", market, 60)

        with mock.patch.object(cbic.requests, "post",
                               side_effect=_mock_post(rates_exc=OSError("down"))):
            with self.assertRaises(cbic.CbicUnavailable):
                cbic.get_notified_rates()

        rate, meta = forex.rate_for("USD")
        self.assertEqual(rate, Decimal("88.400000"))
        self.assertEqual(meta["source"], "cache")

    def test_the_two_rates_are_allowed_to_disagree(self):
        """They will, always, and nothing may reconcile them silently."""
        cache.set("forex:rates:inr", {
            "base": "INR",
            "rates": {"INR": "1.000000", "USD": "88.400000"},
            "fetched_at": "2026-10-24T00:00:00+00:00",
            "provider": "open.er-api.com",
        }, 60)

        with mock.patch.object(cbic.requests, "post", side_effect=_mock_post()):
            notified, _ = cbic.notified_rate_for("USD", cbic.SIDE_IMPORT)
        market, _ = forex.rate_for("USD")

        self.assertNotEqual(notified, market)

"""Paging the Zoho contact book.

The bug these guard against is not a crash. Reading only the first
page of a Zoho list returns 200 rows and reports success, so a
workspace with 500 vendors syncs 200 of them and says so cheerfully.
The missing vendors cannot be matched, and their bills land unassigned
with nothing on screen to explain why.

The vendor sync also used to read the whole contact book unfiltered
and keep the vendors out of it, which on an account whose first page
is all customers synced nothing at all.
"""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.module.zoho.models import ZohoCredentials, ZohoCustomer, ZohoVendor
from apps.module.zoho.views import helpers as zoho_helpers
from apps.module.zoho.views import sync as sync_views
from apps.organizations.models import Organization
from apps.users.models import User


def _contact(n, kind):
    return {
        "contact_id": f"{kind}-{n}",
        "contact_type": kind,
        "company_name": f"{kind.title()} {n}",
        "contact_name": f"{kind.title()} {n} Person",
        "gst_no": "",
        "gst_treatment": "business_gst",
        "place_of_contact": "MH",
    }


class ContactSyncPagingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            email="zsync@example.com", password="x",
            first_name="S", last_name="Y", is_staff=True, is_superuser=True,
        )
        cls.org = Organization.objects.create(
            name="Sync Co", created_by=cls.user, owner=cls.user,
        )
        ZohoCredentials.objects.create(
            organization=cls.org, clientId="c", clientSecret="s",
            accessToken="a", refreshToken="r",
            organisationId="123", is_connected=True,
            # Without a live expiry the view refreshes the token first,
            # and that refresh goes to the real Zoho.
            token_expiry=timezone.now() + timedelta(hours=2),
        )

    def _call(self, view):
        request = APIRequestFactory().post("/sync/")
        force_authenticate(request, user=self.user)
        return view(request, str(self.org.id))

    def _paged(self, rows, per_page=200):
        """Stand in for Zoho, honouring page/per_page as Zoho does."""
        calls = []

        def fake(credentials, endpoint, *args, **kwargs):
            calls.append(endpoint)
            page = int(endpoint.split("page=")[1].split("&")[0])
            start = (page - 1) * per_page
            batch = rows[start:start + per_page]
            return {
                "contacts": batch,
                "page_context": {
                    "page": page,
                    "per_page": per_page,
                    "has_more_page": start + per_page < len(rows),
                },
            }

        return fake, calls

    # -- the loss that looked like success ---------------------------

    def test_vendors_beyond_the_first_page_are_synced(self):
        rows = [_contact(n, "vendor") for n in range(450)]
        fake, calls = self._paged(rows)

        with patch.object(zoho_helpers, "make_zoho_api_request", fake):
            response = self._call(sync_views.vendors_sync_view)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            ZohoVendor.objects.filter(organization=self.org).count(), 450,
        )
        self.assertEqual(len(calls), 3)
        self.assertFalse(response.data["truncated"])

    def test_customers_beyond_the_first_page_are_synced(self):
        rows = [_contact(n, "customer") for n in range(201)]
        fake, _ = self._paged(rows)

        with patch.object(zoho_helpers, "make_zoho_api_request", fake):
            self._call(sync_views.customers_sync_view)

        self.assertEqual(
            ZohoCustomer.objects.filter(organization=self.org).count(), 201,
        )

    # -- asking Zoho for the right half of the book ------------------

    def test_the_vendor_sync_filters_on_zohos_side(self):
        """Unfiltered, a page of customers would sync zero vendors."""
        fake, calls = self._paged([_contact(n, "vendor") for n in range(5)])

        with patch.object(zoho_helpers, "make_zoho_api_request", fake):
            self._call(sync_views.vendors_sync_view)

        self.assertTrue(calls, "no request was made, so the filter is untested")
        self.assertTrue(all("contact_type=vendor" in c for c in calls))

    def test_a_customer_in_the_vendor_response_is_still_rejected(self):
        """The server-side filter is trusted, but not relied upon."""
        fake, _ = self._paged([_contact(1, "vendor"), _contact(2, "customer")])

        with patch.object(zoho_helpers, "make_zoho_api_request", fake):
            self._call(sync_views.vendors_sync_view)

        self.assertEqual(
            list(ZohoVendor.objects.values_list("contactId", flat=True)),
            ["vendor-1"],
        )

    # -- what the operator is told -----------------------------------

    def test_a_vendor_without_a_company_name_keeps_a_usable_label(self):
        """A blank name is a row nobody can pick and no bill can match."""
        row = _contact(1, "vendor")
        row["company_name"] = ""
        fake, _ = self._paged([row])

        with patch.object(zoho_helpers, "make_zoho_api_request", fake):
            self._call(sync_views.vendors_sync_view)

        self.assertEqual(ZohoVendor.objects.get().companyName, "Vendor 1 Person")

    def test_a_resync_reports_updates_rather_than_zero(self):
        """A "synced 0" on an unchanged list reads as a failure."""
        fake, _ = self._paged([_contact(n, "vendor") for n in range(3)])

        with patch.object(zoho_helpers, "make_zoho_api_request", fake):
            self._call(sync_views.vendors_sync_view)
            response = self._call(sync_views.vendors_sync_view)

        self.assertEqual(response.data["synced_count"], 0)
        self.assertEqual(response.data["updated_count"], 3)
        self.assertIn("3 updated", response.data["detail"])
        self.assertEqual(ZohoVendor.objects.count(), 3)

    def test_an_empty_list_says_so_instead_of_claiming_a_sync(self):
        fake, _ = self._paged([])

        with patch.object(zoho_helpers, "make_zoho_api_request", fake):
            response = self._call(sync_views.vendors_sync_view)

        self.assertIn("no vendors", response.data["detail"])

    def test_an_unpaged_response_is_taken_as_complete(self):
        """Not every Zoho collection reports a page_context."""
        rows = [_contact(n, "vendor") for n in range(4)]

        def fake(credentials, endpoint, *args, **kwargs):
            return {"contacts": rows}

        with patch.object(zoho_helpers, "make_zoho_api_request", fake):
            response = self._call(sync_views.vendors_sync_view)

        self.assertEqual(ZohoVendor.objects.count(), 4)
        self.assertFalse(response.data["truncated"])

    def test_a_long_field_is_truncated_rather_than_raising(self):
        """Zoho's limits are longer than the local columns."""
        row = _contact(1, "vendor")
        row["company_name"] = "N" * 300
        row["gst_no"] = "G" * 60
        fake, _ = self._paged([row])

        with patch.object(zoho_helpers, "make_zoho_api_request", fake):
            response = self._call(sync_views.vendors_sync_view)

        self.assertEqual(response.status_code, 200)
        vendor = ZohoVendor.objects.get()
        self.assertEqual(len(vendor.companyName), 100)
        self.assertEqual(len(vendor.gstNo), 30)


class PagingCeilingTests(TestCase):
    """A server that always says "more" must not loop for ever."""

    def test_the_page_ceiling_is_reported_not_hidden(self):
        from apps.module.zoho.views import helpers

        def fake(credentials, endpoint, *args, **kwargs):
            return {
                "contacts": [_contact(1, "vendor")],
                "page_context": {"has_more_page": True},
            }

        with patch.object(helpers, "make_zoho_api_request", fake):
            rows, truncated = helpers.fetch_all_zoho_pages(
                object(), "contacts", "contacts",
            )

        self.assertTrue(truncated)
        self.assertEqual(len(rows), helpers._MAX_PAGES)

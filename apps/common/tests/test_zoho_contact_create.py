"""Creating a vendor or customer in the client's Zoho Books.

This endpoint writes a master into someone else's accounting system, so
most of what is guarded here is restraint rather than function.

A duplicated vendor is the failure that matters. It splits the ledger
and the ageing report, and nobody notices for a month — which makes it
worse than the unmapped bill the feature exists to fix. So the tests
care most about the checks that run *before* anything is created, and
about the fact that a garbled GSTIN never becomes a vendor.
"""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.module.zoho.models import ZohoCredentials, ZohoCustomer, ZohoVendor
from apps.module.zoho.views import contacts as contacts_view
from apps.module.zoho.views.contacts import normalise_company_name
from apps.organizations.models import Organization, OrgMembership
from apps.users.models import User

GOOD_GSTIN = "27AAAAA0000A1Z5"


class ContactCreateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            email="cc@example.com", password="x", first_name="C", last_name="C",
            is_staff=True, is_superuser=True,
        )
        cls.org = Organization.objects.create(
            name="Create Co", created_by=cls.user, owner=cls.user,
        )

    def setUp(self):
        ZohoCredentials.objects.update_or_create(
            organization=self.org,
            defaults={
                "clientId": "c", "clientSecret": "s",
                "accessToken": "a", "refreshToken": "r",
                "organisationId": "123", "is_connected": True,
                # Without a live expiry the view refreshes the token
                # first, and that refresh goes to the real Zoho —
                # which fails and clears is_connected.
                "token_expiry": timezone.now() + timedelta(hours=2),
            },
        )

    def _post(self, body, user=None):
        request = APIRequestFactory().post(
            "/contacts/create/", body, format="json",
        )
        force_authenticate(request, user=user or self.user)
        return contacts_view.zoho_contact_create_view(request, str(self.org.id))

    def _zoho(self, created_id="z-1", search_results=None):
        """Stand in for Zoho: a name search, then the create."""
        calls = []

        def fake(credentials, endpoint, method="GET", data=None):
            calls.append((method, endpoint, data))
            if method == "POST":
                return {"contact": {
                    "contact_id": created_id,
                    "company_name": (data or {}).get("company_name"),
                    "gst_no": (data or {}).get("gst_no", ""),
                    "gst_treatment": (data or {}).get("gst_treatment"),
                }}
            return {"contacts": search_results or []}

        return fake, calls

    # -- the duplicate guard -----------------------------------------

    def test_a_matching_gstin_stops_the_create_and_offers_the_existing_one(self):
        """The only identifier that is actually unique."""
        existing = ZohoVendor.objects.create(
            organization=self.org, contactId="old-1",
            companyName="Acme Traders", gstNo=GOOD_GSTIN,
        )
        fake, calls = self._zoho()

        with patch.object(contacts_view, "make_zoho_api_request", fake):
            response = self._post({
                "contact_type": "vendor",
                "contact_name": "Totally Different Name",
                "gst_no": GOOD_GSTIN,
            })

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.data["existing_local"][0]["contact_id"], existing.contactId,
        )
        self.assertEqual(
            response.data["existing_local"][0]["matched_on"], "same GSTIN",
        )
        self.assertFalse([c for c in calls if c[0] == "POST"])

    def test_a_matching_name_stops_the_create_despite_a_legal_suffix(self):
        """"Acme Traders Pvt Ltd" is not a second supplier."""
        ZohoVendor.objects.create(
            organization=self.org, contactId="old-1",
            companyName="Acme Traders", gstNo="",
        )
        fake, _ = self._zoho()

        with patch.object(contacts_view, "make_zoho_api_request", fake):
            response = self._post({
                "contact_type": "vendor",
                "contact_name": "ACME TRADERS PVT LTD",
            })

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.data["existing_local"][0]["matched_on"], "same name",
        )

    def test_a_contact_in_zoho_but_not_synced_locally_still_stops_it(self):
        fake, _ = self._zoho(search_results=[{
            "contact_id": "z-existing",
            "company_name": "Acme Traders",
            "gst_no": GOOD_GSTIN,
            "status": "active",
        }])

        with patch.object(contacts_view, "make_zoho_api_request", fake):
            response = self._post({
                "contact_type": "vendor", "contact_name": "Acme Traders",
            })

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.data["existing_in_zoho"][0]["contact_id"], "z-existing",
        )
        self.assertEqual(ZohoVendor.objects.count(), 0)

    def test_force_creates_anyway(self):
        """A second, deliberate call — never the first one."""
        ZohoVendor.objects.create(
            organization=self.org, contactId="old-1",
            companyName="Acme Traders", gstNo=GOOD_GSTIN,
        )
        fake, _ = self._zoho(created_id="z-2")

        with patch.object(contacts_view, "make_zoho_api_request", fake):
            response = self._post({
                "contact_type": "vendor", "contact_name": "Acme Traders",
                "gst_no": GOOD_GSTIN, "force": True,
            })

        self.assertEqual(response.status_code, 201)
        self.assertEqual(ZohoVendor.objects.count(), 2)

    def test_a_search_failure_does_not_block_the_create(self):
        """A duplicate *check* that cannot reach Zoho must not veto work."""
        def fake(credentials, endpoint, method="GET", data=None):
            if method == "POST":
                return {"contact": {"contact_id": "z-9",
                                    "company_name": "Fresh Co"}}
            raise RuntimeError("search down")

        with patch.object(contacts_view, "make_zoho_api_request", fake):
            response = self._post({
                "contact_type": "vendor", "contact_name": "Fresh Co",
            })

        self.assertEqual(response.status_code, 201)
        self.assertIn("could not be searched", response.data["search_warning"])

    # -- a garbled read must not become a vendor ----------------------

    def test_an_invalid_gstin_is_refused_before_anything_is_created(self):
        fake, calls = self._zoho()

        with patch.object(contacts_view, "make_zoho_api_request", fake):
            response = self._post({
                "contact_type": "vendor", "contact_name": "Acme",
                "gst_no": "27AAAAA0000A1Z",  # one character short
            })

        self.assertEqual(response.status_code, 400)
        self.assertIn("not a valid GSTIN", response.data["detail"])
        self.assertEqual(calls, [])

    def test_a_gstin_with_stray_spaces_is_accepted(self):
        """OCR puts spaces in; that is not a reason to refuse the number."""
        fake, calls = self._zoho()

        with patch.object(contacts_view, "make_zoho_api_request", fake):
            response = self._post({
                "contact_type": "vendor", "contact_name": "Acme",
                "gst_no": " 27aaaaa0000a1z5 ", "force": True,
            })

        self.assertEqual(response.status_code, 201)
        posted = [c for c in calls if c[0] == "POST"][0][2]
        self.assertEqual(posted["gst_no"], GOOD_GSTIN)

    # -- what Zoho is actually sent -----------------------------------

    def test_the_gst_treatment_follows_from_whether_there_is_a_number(self):
        fake, calls = self._zoho()

        with patch.object(contacts_view, "make_zoho_api_request", fake):
            self._post({"contact_type": "vendor", "contact_name": "No GST Co"})

        posted = [c for c in calls if c[0] == "POST"][0][2]
        self.assertEqual(posted["gst_treatment"], "business_none")

    def test_claiming_gst_registration_without_a_number_is_refused(self):
        response = self._post({
            "contact_type": "vendor", "contact_name": "Acme",
            "gst_treatment": "business_gst",
        })

        self.assertEqual(response.status_code, 400)
        self.assertIn("needs its GSTIN", response.data["detail"])

    def test_an_unknown_gst_treatment_is_refused_here_not_by_zoho(self):
        response = self._post({
            "contact_type": "vendor", "contact_name": "Acme",
            "gst_treatment": "whatever",
        })

        self.assertEqual(response.status_code, 400)
        self.assertIn("business_gst", response.data["detail"])

    # -- the round trip this exists to remove -------------------------

    def test_the_new_contact_is_recorded_locally_without_a_resync(self):
        fake, _ = self._zoho(created_id="z-77")

        with patch.object(contacts_view, "make_zoho_api_request", fake):
            response = self._post({
                "contact_type": "vendor", "contact_name": "Brand New Co",
                "gst_no": GOOD_GSTIN,
            })

        self.assertEqual(response.status_code, 201)
        row = ZohoVendor.objects.get()
        self.assertEqual(row.contactId, "z-77")
        self.assertEqual(row.gstNo, GOOD_GSTIN)
        self.assertEqual(str(row.id), response.data["id"])

    def test_a_customer_keeps_its_place_of_supply(self):
        def fake(credentials, endpoint, method="GET", data=None):
            if method == "POST":
                return {"contact": {
                    "contact_id": "z-5",
                    "company_name": data["company_name"],
                    "place_of_contact": "MH",
                }}
            return {"contacts": []}

        with patch.object(contacts_view, "make_zoho_api_request", fake):
            self._post({
                "contact_type": "customer", "contact_name": "Beta Buyers",
                "place_of_contact": "mh",
            })

        self.assertEqual(ZohoCustomer.objects.get().place_of_supply, "MH")

    def test_zoho_returning_no_id_is_not_reported_as_success(self):
        """Nothing to select and nothing to record is not a create."""
        def fake(credentials, endpoint, method="GET", data=None):
            if method == "POST":
                return {"contact": {}}
            return {"contacts": []}

        with patch.object(contacts_view, "make_zoho_api_request", fake):
            response = self._post({
                "contact_type": "vendor", "contact_name": "Ghost Co",
            })

        self.assertEqual(response.status_code, 502)
        self.assertEqual(ZohoVendor.objects.count(), 0)

    # -- refusing the request itself ----------------------------------

    def test_a_disconnected_workspace_is_told_to_connect_first(self):
        ZohoCredentials.objects.filter(organization=self.org).update(
            is_connected=False, organisationId="",
        )

        response = self._post({
            "contact_type": "vendor", "contact_name": "Acme",
        })

        self.assertEqual(response.status_code, 400)
        self.assertIn("not connected", response.data["detail"])

    def test_a_missing_name_is_refused(self):
        response = self._post({"contact_type": "vendor", "contact_name": "  "})

        self.assertEqual(response.status_code, 400)
        self.assertIn("contact_name is required", response.data["detail"])

    def test_chart_of_accounts_is_not_a_contact_type(self):
        """Structural masters are deliberately not creatable here."""
        response = self._post({
            "contact_type": "chart_of_accounts", "contact_name": "Purchases",
        })

        self.assertEqual(response.status_code, 400)
        self.assertIn("contact_type must be one of", response.data["detail"])

    def test_an_accountant_may_not_create_masters(self):
        """They can map bills; they do not shape the client's books."""
        accountant = User.objects.create_user(
            email="acct@example.com", password="x",
            first_name="A", last_name="C",
        )
        OrgMembership.objects.create(
            user=accountant, organization=self.org,
            role=OrgMembership.ACCOUNTANT, is_active=True,
        )

        response = self._post(
            {"contact_type": "vendor", "contact_name": "Acme"}, user=accountant,
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(ZohoVendor.objects.count(), 0)


class NameNormalisationTests(TestCase):
    """The comparison that decides whether a duplicate is offered."""

    def test_legal_suffixes_do_not_make_a_second_supplier(self):
        for variant in (
            "Acme Traders", "ACME TRADERS", "Acme  Traders",
            "Acme Traders Pvt Ltd", "Acme Traders Private Limited",
            "Acme-Traders, LLP", "Acme Traders Co.",
        ):
            self.assertEqual(
                normalise_company_name(variant), "acme traders", variant,
            )

    def test_genuinely_different_names_stay_different(self):
        self.assertNotEqual(
            normalise_company_name("Acme Traders"),
            normalise_company_name("Acme Trading"),
        )
        self.assertNotEqual(
            normalise_company_name("Acme Traders"),
            normalise_company_name("Acme Traders Mumbai"),
        )

    def test_an_empty_name_normalises_to_nothing(self):
        self.assertEqual(normalise_company_name(None), "")
        self.assertEqual(normalise_company_name("   "), "")

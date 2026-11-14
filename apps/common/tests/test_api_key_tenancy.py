"""An organisation API key must not reach another organisation.

``OrganizationAPIKeyOrBearerToken`` used to validate the key, stamp
``request.organization`` and return True — without ever comparing that
organisation to the ``org_id`` in the URL.

Views that resolve the org through ``get_organization_from_request``
were saved by the check inside it. The Tally configuration surface does
not: it calls ``get_object_or_404(Organization, id=org_id)`` and takes
the URL at its word. The result was a cross-tenant hole on six
endpoints, three of which write:

    READ  victim config      -> HTTP 200   leaked: ['Victim Sundry Debtors']
    WRITE victim config      -> HTTP 200   victim now has: WIPED
    READ  victim gst maps    -> HTTP 200

One paying client's API key could read, overwrite and wipe another
paying client's Tally configuration.

Fixed in the permission class rather than in each view, so every
endpoint that uses it — now and later — is covered.
"""

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_api_key.models import APIKey

from apps.module.tally.models import ParentLedger, TallyConfig
from apps.organizations.models import (
    Organization, OrganizationAPIKey, OrgMembership,
)

User = get_user_model()


class ApiKeyTenancyTests(APITestCase):
    """The key says who you are, not who you asked for."""

    @classmethod
    def setUpTestData(cls):
        cls.attacker = User.objects.create_user(
            email="attacker@tenancy.test", password="x-test-pass-123",
        )
        cls.org_a = Organization.objects.create(
            name="Attacker Org", owner=cls.attacker, created_by=cls.attacker,
        )
        OrgMembership.objects.create(
            organization=cls.org_a, user=cls.attacker, role=OrgMembership.ADMIN,
        )

        cls.victim = User.objects.create_user(
            email="victim@tenancy.test", password="x-test-pass-123",
        )
        cls.org_b = Organization.objects.create(
            name="Victim Org", owner=cls.victim, created_by=cls.victim,
        )
        OrgMembership.objects.create(
            organization=cls.org_b, user=cls.victim, role=OrgMembership.ADMIN,
        )

        cls.victim_config = TallyConfig.objects.create(organization=cls.org_b)
        cls.victim_parent = ParentLedger.objects.create(
            organization=cls.org_b, parent="Victim Sundry Debtors",
        )
        cls.victim_config.customer_parents.set([cls.victim_parent])

        api_obj, cls.attacker_key = APIKey.objects.create_key(name="attacker key")
        OrganizationAPIKey.objects.create(
            api_key=api_obj, organization=cls.org_a,
            name="attacker", created_by=cls.attacker,
        )

        own_obj, cls.own_key = APIKey.objects.create_key(name="victim key")
        OrganizationAPIKey.objects.create(
            api_key=own_obj, organization=cls.org_b,
            name="victim", created_by=cls.victim,
        )

    def as_key(self, key):
        self.client.credentials(HTTP_AUTHORIZATION=f"Api-Key {key}")

    def url(self, name, org, **kwargs):
        return reverse(f"tally:{name}", args=[org.id, *kwargs.values()])

    # -- the hole ------------------------------------------------------
    def test_another_orgs_config_cannot_be_read(self):
        self.as_key(self.attacker_key)
        response = self.client.get(self.url("get-tally-config", self.org_b))
        self.assertNotEqual(
            response.status_code, status.HTTP_200_OK,
            "an API key read another organisation's Tally configuration",
        )

    def test_another_orgs_config_cannot_be_written(self):
        self.as_key(self.attacker_key)
        response = self.client.post(
            self.url("create-update-tally-config", self.org_b),
            {"customer_parents": []}, format="json",
        )
        self.assertNotEqual(response.status_code, status.HTTP_200_OK)

        self.victim_config.refresh_from_db()
        self.assertEqual(
            list(self.victim_config.customer_parents.all()), [self.victim_parent],
            "another organisation's configuration was overwritten",
        )

    def test_another_orgs_gst_mappings_cannot_be_read(self):
        self.as_key(self.attacker_key)
        response = self.client.get(
            self.url("gst-rate-mappings-list", self.org_b),
        )
        self.assertNotEqual(response.status_code, status.HTTP_200_OK)

    # -- and the key must still work where it should -------------------
    def test_a_key_still_reads_its_own_org(self):
        """The Tally connector authenticates this way; it must keep working."""
        self.as_key(self.own_key)
        response = self.client.get(self.url("get-tally-config", self.org_b))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content[:200])

    def test_a_key_still_writes_its_own_org(self):
        self.as_key(self.own_key)
        response = self.client.post(
            self.url("create-update-tally-config", self.org_b),
            {"customer_parents": [str(self.victim_parent.id)]}, format="json",
        )
        self.assertIn(
            response.status_code,
            (status.HTTP_200_OK, status.HTTP_201_CREATED), response.content[:200],
        )
        self.victim_config.refresh_from_db()
        self.assertEqual(
            list(self.victim_config.customer_parents.all()), [self.victim_parent],
        )

    def test_a_revoked_or_unknown_key_is_refused(self):
        self.as_key("this-is-not-a-key")
        response = self.client.get(self.url("get-tally-config", self.org_a))
        self.assertNotEqual(response.status_code, status.HTTP_200_OK)

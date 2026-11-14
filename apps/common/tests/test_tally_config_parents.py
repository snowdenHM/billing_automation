"""Every parent-ledger group on TallyConfig must survive a save.

The config endpoint used to carry its own hand-written list of which
parent groups it would write. Four fields added for the sales and
receipt vouchers — ``customer_parents``, ``sales_coa_parents``,
``receipt_parents`` and ``rcm_parents`` — were never added to it, so a
save answered ``200 {"success": true, "message": "...updated
successfully"}`` and wrote nothing at all.

Nothing downstream could tell the difference between "the operator has
not configured this yet" and "the operator configured it four times and
we threw it away". The setup page showed a success toast, re-read the
config, and drew the same empty groups. The ledger dropdowns the sales
and receipt pages depend on answered ``"No customer_parents configured
in TallyConfig"``, so the customer picker, the sales-ledger picker and
the bank/cash picker were all empty — and a customer created from the
"Add New Customer" dialog was saved correctly but never appeared in the
list it was created for.

The test is written against the model rather than against a list of
names, so a group added later is covered the day it is added.
"""

import uuid

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.module.tally.models import ParentLedger, TallyConfig
from apps.organizations.models import Organization, OrgMembership

User = get_user_model()


def parent_group_fields():
    """The names of every ParentLedger many-to-many on TallyConfig."""
    return [
        field.name
        for field in TallyConfig._meta.many_to_many
        if field.related_model is ParentLedger
    ]


class TallyConfigParentSaveTests(APITestCase):
    """A saved parent group is a parent group that comes back."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            email="config@tally.test", password="x-test-pass-123",
        )
        cls.org = Organization.objects.create(
            name="Config Org", owner=cls.user, created_by=cls.user,
        )
        OrgMembership.objects.create(
            organization=cls.org, user=cls.user, role=OrgMembership.ADMIN,
        )
        # One parent ledger per group, so each save has something of its
        # own to point at and a leak between groups would show up.
        cls.parents = {
            name: ParentLedger.objects.create(
                organization=cls.org, parent=f"Group For {name}",
            )
            for name in parent_group_fields()
        }

    def setUp(self):
        self.client.force_authenticate(self.user)

    def url(self):
        return reverse("tally:create-update-tally-config", args=[self.org.id])

    def test_every_parent_group_is_saved(self):
        """Post every group at once; each one must read back."""
        payload = {
            name: [str(parent.id)] for name, parent in self.parents.items()
        }

        response = self.client.post(self.url(), payload, format="json")
        self.assertIn(
            response.status_code,
            (status.HTTP_200_OK, status.HTTP_201_CREATED),
            response.data,
        )

        config = TallyConfig.objects.get(organization=self.org)
        for name, parent in self.parents.items():
            saved = list(getattr(config, name).values_list("id", flat=True))
            self.assertEqual(
                saved, [parent.id],
                f"{name} was accepted by the endpoint but not written. The "
                f"endpoint must take its field list from the model, not "
                f"from a list maintained by hand.",
            )

    def test_the_sales_side_groups_are_covered(self):
        """The four that were missing, named, so the regression is legible."""
        for name in (
            "customer_parents",
            "sales_coa_parents",
            "receipt_parents",
            "rcm_parents",
        ):
            self.assertIn(
                name, parent_group_fields(),
                f"{name} is no longer a ParentLedger group on TallyConfig — "
                f"if it was renamed, rename it here too.",
            )

    def test_a_group_can_be_cleared(self):
        """An empty list means "none", not "leave what was there"."""
        self.client.post(
            self.url(),
            {"customer_parents": [str(self.parents["customer_parents"].id)]},
            format="json",
        )
        config = TallyConfig.objects.get(organization=self.org)
        self.assertEqual(config.customer_parents.count(), 1)

        self.client.post(self.url(), {"customer_parents": []}, format="json")
        config.refresh_from_db()
        self.assertEqual(config.customer_parents.count(), 0)

    def test_a_group_left_out_is_left_alone(self):
        """A partial save must not wipe the groups it says nothing about.

        The setup page saves one section at a time, so a payload carrying
        only the vendor group must leave the customer group standing.
        """
        self.client.post(
            self.url(),
            {
                "customer_parents": [str(self.parents["customer_parents"].id)],
                "vendor_parents": [str(self.parents["vendor_parents"].id)],
            },
            format="json",
        )

        self.client.post(
            self.url(),
            {"vendor_parents": [str(self.parents["vendor_parents"].id)]},
            format="json",
        )

        config = TallyConfig.objects.get(organization=self.org)
        self.assertEqual(
            config.customer_parents.count(), 1,
            "A save that did not mention customer_parents cleared it.",
        )

    def test_a_parent_from_another_organisation_is_refused(self):
        """Ids are filtered by organisation, not taken on trust."""
        other_owner = User.objects.create_user(
            email="other@tally.test", password="x-test-pass-123",
        )
        other_org = Organization.objects.create(
            name="Other", owner=other_owner, created_by=other_owner,
        )
        stranger = ParentLedger.objects.create(
            organization=other_org, parent="Someone Else's Debtors",
        )

        self.client.post(
            self.url(), {"customer_parents": [str(stranger.id)]}, format="json",
        )

        config = TallyConfig.objects.get(organization=self.org)
        self.assertEqual(
            config.customer_parents.count(), 0,
            "A ParentLedger belonging to another organisation was saved.",
        )

    def test_an_unknown_id_does_not_break_the_save(self):
        """A stale id in the payload is dropped, not raised."""
        response = self.client.post(
            self.url(),
            {
                "customer_parents": [
                    str(self.parents["customer_parents"].id),
                    str(uuid.uuid4()),
                ],
            },
            format="json",
        )
        self.assertIn(
            response.status_code,
            (status.HTTP_200_OK, status.HTTP_201_CREATED),
            response.data,
        )
        config = TallyConfig.objects.get(organization=self.org)
        self.assertEqual(config.customer_parents.count(), 1)

"""The sales-side adjustment ledgers save, and come back.

Client correction, 2026-11-18: "Discount on purchases goes to one
ledger and Discount on Sales goes to another ledger, so user can select
that."

``TallyConfig`` carried one ledger per adjustment — cess, discount,
freight, round off, TDS — and one slot can only be right for one side
of the books. A discount a supplier gives us is income; a discount we
give a customer is an expense. Each adjustment now has a ``_sales``
twin; the original five keep their names and stay the purchase side, so
nothing an organisation has already configured changes meaning.

The save endpoint used to list these field names by hand, in three
separate places. It derives them from the model now. The hand-written
parent-group list beside it had already fallen four fields behind and
dropped saved configuration while answering "updated successfully" —
this test exists so the same thing cannot happen to these.
"""

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.module.tally.models import Ledger, ParentLedger, TallyConfig
from apps.module.tally.views.config import adjustment_ledger_fields
from apps.organizations.models import OrgMembership, Organization

User = get_user_model()


class SalesAdjustmentLedgerTests(APITestCase):

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            email="adjust@tally.test", password="x-test-pass-123",
        )
        cls.org = Organization.objects.create(
            name="Adjust Org", owner=cls.user, created_by=cls.user,
        )
        OrgMembership.objects.create(
            organization=cls.org, user=cls.user, role=OrgMembership.ADMIN,
        )
        parent = ParentLedger.objects.create(
            organization=cls.org, parent="Indirect Expenses",
        )
        cls.ledgers = {
            name: Ledger.objects.create(
                organization=cls.org, name=name, parent=parent,
            )
            for name in ("Discount Received", "Discount Allowed",
                         "Freight Inward", "Freight Outward")
        }

    def setUp(self):
        self.client.force_authenticate(user=self.user)

    @property
    def url(self):
        return reverse("tally:create-update-tally-config", args=[self.org.id])

    def test_the_model_carries_a_sales_twin_for_every_adjustment(self):
        fields = set(adjustment_ledger_fields())
        for name in ("cess", "discount", "freight", "round_off", "tds"):
            self.assertIn(f"{name}_ledger", fields)
            self.assertIn(f"{name}_ledger_sales", fields)

    def test_both_sides_save_independently(self):
        response = self.client.post(self.url, {
            "discount_ledger": str(self.ledgers["Discount Received"].id),
            "discount_ledger_sales": str(self.ledgers["Discount Allowed"].id),
            "freight_ledger": str(self.ledgers["Freight Inward"].id),
            "freight_ledger_sales": str(self.ledgers["Freight Outward"].id),
        }, format="json")
        self.assertIn(
            response.status_code,
            (status.HTTP_200_OK, status.HTTP_201_CREATED), response.data,
        )
        config = TallyConfig.objects.get(organization=self.org)
        self.assertEqual(config.discount_ledger, self.ledgers["Discount Received"])
        self.assertEqual(
            config.discount_ledger_sales, self.ledgers["Discount Allowed"],
            "the sales-side discount ledger was dropped by the save",
        )
        self.assertEqual(config.freight_ledger, self.ledgers["Freight Inward"])
        self.assertEqual(config.freight_ledger_sales, self.ledgers["Freight Outward"])

    def test_the_saved_values_are_returned(self):
        """Saved and never shown is this project's recurring failure."""
        self.client.post(self.url, {
            "discount_ledger_sales": str(self.ledgers["Discount Allowed"].id),
        }, format="json")
        response = self.client.get(
            reverse("tally:get-tally-config", args=[self.org.id])
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data.get("data", response.data)
        self.assertEqual(
            data["discount_ledger_sales"], self.ledgers["Discount Allowed"].id,
        )
        self.assertEqual(data["discount_ledger_sales_name"], "Discount Allowed")

    def test_saving_one_side_leaves_the_other_alone(self):
        """A partial save must not clear what it did not mention."""
        self.client.post(self.url, {
            "discount_ledger": str(self.ledgers["Discount Received"].id),
        }, format="json")
        self.client.post(self.url, {
            "discount_ledger_sales": str(self.ledgers["Discount Allowed"].id),
        }, format="json")
        config = TallyConfig.objects.get(organization=self.org)
        self.assertEqual(
            config.discount_ledger, self.ledgers["Discount Received"],
            "saving the sales ledger cleared the purchase one",
        )
        self.assertEqual(config.discount_ledger_sales, self.ledgers["Discount Allowed"])

    def test_another_organizations_ledger_is_refused(self):
        other = User.objects.create_user(
            email="elsewhere@tally.test", password="x-test-pass-123",
        )
        other_org = Organization.objects.create(
            name="Elsewhere", owner=other, created_by=other,
        )
        foreign = Ledger.objects.create(
            organization=other_org, name="Their Discount",
            parent=ParentLedger.objects.create(
                organization=other_org, parent="Indirect Expenses",
            ),
        )
        self.client.post(self.url, {
            "discount_ledger_sales": str(foreign.id),
        }, format="json")
        config = TallyConfig.objects.get(organization=self.org)
        self.assertIsNone(
            config.discount_ledger_sales,
            "another organisation's ledger was accepted",
        )

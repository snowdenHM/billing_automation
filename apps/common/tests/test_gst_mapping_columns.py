"""The GST rate mapping saves six ledgers, and only this org's.

Two things, both touched by the same client correction.

**The six columns.** The mapping carried one ledger per tax per rate,
and sales and purchases both read it, so an output tax could post to an
input-credit ledger. The three existing columns stay the input side and
three ``*_output_*`` columns are added — see
``test_tax_ledger_direction`` for what reads them.

The save endpoint used to name its three columns by hand. The list is
derived from the model now, for the same reason the parent-group list
had to be: a hand-written copy of a field list in this project has
already fallen behind once and dropped four groups' worth of saved
configuration while answering 200.

**Whose ledgers.** The ids were taken on trust. Nothing compared them
to the organisation in the URL, so a caller could map another tenant's
ledger into their own GST configuration by id alone — the same class of
hole as the API-key one in ``test_api_key_tenancy``, reached through a
different door.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.module.tally.models import (
    GstRateLedgerMapping, Ledger, ParentLedger,
)
from apps.organizations.models import OrgMembership, Organization

User = get_user_model()


class GstMappingColumnTests(APITestCase):

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            email="mapping@tally.test", password="x-test-pass-123",
        )
        cls.org = Organization.objects.create(
            name="Mapping Org", owner=cls.user, created_by=cls.user,
        )
        OrgMembership.objects.create(
            organization=cls.org, user=cls.user, role=OrgMembership.ADMIN,
        )
        duties = ParentLedger.objects.create(
            organization=cls.org, parent="Duties & Taxes",
        )
        cls.ledgers = {
            name: Ledger.objects.create(
                organization=cls.org, name=name, parent=duties,
            )
            for name in (
                "Input CGST 18%", "Output CGST 18%",
                "Input SGST 18%", "Output SGST 18%",
                "Input IGST 18%", "Output IGST 18%",
            )
        }

        # A second tenant, whose ledger must not be reachable.
        other_user = User.objects.create_user(
            email="other@tally.test", password="x-test-pass-123",
        )
        cls.other_org = Organization.objects.create(
            name="Other Org", owner=other_user, created_by=other_user,
        )
        other_parent = ParentLedger.objects.create(
            organization=cls.other_org, parent="Duties & Taxes",
        )
        cls.foreign_ledger = Ledger.objects.create(
            organization=cls.other_org, name="Someone Else's IGST",
            parent=other_parent,
        )

    def setUp(self):
        self.client.force_authenticate(user=self.user)

    @property
    def url(self):
        return reverse("tally:gst-rate-mappings-upsert", args=[self.org.id])

    def save(self, entry):
        return self.client.post(self.url, {"mappings": [entry]}, format="json")

    # -- the six columns -----------------------------------------------
    def test_all_six_ledgers_are_saved(self):
        response = self.save({
            "rate": "18.00",
            "cgst_ledger": str(self.ledgers["Input CGST 18%"].id),
            "sgst_ledger": str(self.ledgers["Input SGST 18%"].id),
            "igst_ledger": str(self.ledgers["Input IGST 18%"].id),
            "cgst_output_ledger": str(self.ledgers["Output CGST 18%"].id),
            "sgst_output_ledger": str(self.ledgers["Output SGST 18%"].id),
            "igst_output_ledger": str(self.ledgers["Output IGST 18%"].id),
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        mapping = GstRateLedgerMapping.objects.get(
            organization=self.org, rate=Decimal("18.00"),
        )
        for field, name in (
            ("cgst_ledger", "Input CGST 18%"),
            ("sgst_ledger", "Input SGST 18%"),
            ("igst_ledger", "Input IGST 18%"),
            ("cgst_output_ledger", "Output CGST 18%"),
            ("sgst_output_ledger", "Output SGST 18%"),
            ("igst_output_ledger", "Output IGST 18%"),
        ):
            self.assertEqual(
                getattr(mapping, field), self.ledgers[name],
                f"{field} was dropped by the save",
            )

    def test_the_saved_columns_come_back(self):
        """Saved, stored and never shown is the failure mode here."""
        self.save({
            "rate": "18.00",
            "igst_output_ledger": str(self.ledgers["Output IGST 18%"].id),
        })
        listing = self.client.get(
            reverse("tally:gst-rate-mappings-list", args=[self.org.id])
        )
        self.assertEqual(listing.status_code, status.HTTP_200_OK)
        rows = listing.data.get("data", listing.data)
        row = next(r for r in rows if Decimal(str(r["rate"])) == Decimal("18.00"))
        self.assertEqual(
            row["igst_output_ledger"], self.ledgers["Output IGST 18%"].id,
        )
        self.assertEqual(row["igst_output_ledger_name"], "Output IGST 18%")

    def test_an_omitted_column_is_cleared(self):
        self.save({
            "rate": "18.00",
            "igst_output_ledger": str(self.ledgers["Output IGST 18%"].id),
        })
        self.save({"rate": "18.00"})
        mapping = GstRateLedgerMapping.objects.get(
            organization=self.org, rate=Decimal("18.00"),
        )
        self.assertIsNone(mapping.igst_output_ledger)

    # -- whose ledgers --------------------------------------------------
    def test_another_organizations_ledger_is_refused(self):
        response = self.save({
            "rate": "18.00",
            "igst_ledger": str(self.foreign_ledger.id),
        })
        mapping = GstRateLedgerMapping.objects.filter(
            organization=self.org, rate=Decimal("18.00"),
        ).first()
        self.assertTrue(
            mapping is None or mapping.igst_ledger is None,
            "another organisation's ledger was written into this org's "
            "GST configuration",
        )
        self.assertTrue(
            response.data.get("errors"),
            "the rejection was silent — the operator would see a success",
        )

    def test_a_valid_column_still_saves_beside_a_refused_one(self):
        self.save({
            "rate": "18.00",
            "igst_ledger": str(self.foreign_ledger.id),
            "cgst_ledger": str(self.ledgers["Input CGST 18%"].id),
        })
        mapping = GstRateLedgerMapping.objects.get(
            organization=self.org, rate=Decimal("18.00"),
        )
        self.assertIsNone(mapping.igst_ledger)
        self.assertEqual(mapping.cgst_ledger, self.ledgers["Input CGST 18%"])

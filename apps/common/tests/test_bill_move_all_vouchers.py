"""A bill can be moved between any two Tally vouchers.

Client correction 15: "Add a button Move to option for Receipt, Payment
and Sales voucher."

The button was missing because the endpoint behind it was written as
if/elif pairs for vendor and expense and nothing else. Payment carried
``allowMove={false}`` with a comment saying there was no correct value
to send; Receipt and Sales had the same, each with a reason invented to
explain the gap ("a receipt has nowhere to go"). A payment filed as a
purchase had to be deleted and uploaded again.

The five vouchers are described in one registry now and the move reads
from it, so every pair works and a sixth would be one entry.

What a move carries, and what it cannot:

* The document, its status, and everything extracted from it.
* The party, only when both sides keep it in the same place. A supplier
  ledger is not a customer ledger, so purchase -> sales leaves the
  party blank rather than writing a creditor into a debtor field.
* Line items, translated between the two shapes — an ITEM voucher
  carries goods, a LEDGER voucher carries postings.
"""

from decimal import Decimal
from itertools import permutations

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.module.tally.views.bill_move import MODULES
from apps.organizations.models import OrgMembership, Organization

User = get_user_model()


class MoveBetweenAllVouchersTests(APITestCase):

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            email="move@tally.test", password="x-test-pass-123",
        )
        cls.org = Organization.objects.create(
            name="Move Org", owner=cls.user, created_by=cls.user,
        )
        OrgMembership.objects.create(
            organization=cls.org, user=cls.user, role=OrgMembership.ADMIN,
        )

    def setUp(self):
        self.client.force_authenticate(user=self.user)

    @property
    def url(self):
        return reverse("tally:tally-bills-move", args=[self.org.id])

    def make_bill(self, module, status_value="Analysed"):
        spec = MODULES[module]
        bill = spec["bill"].objects.create(
            organization=self.org,
            status=status_value,
            analysed_data={"invoice_number": "MOVE-1"},
        )
        analysed = spec["analysed"].objects.create(
            organization=self.org,
            selected_bill=bill,
            bill_no="MOVE-1",
            total=Decimal("1000.00"),
        )
        row = {
            spec["fk"]: analysed,
            "organization": self.org,
            "amount": Decimal("1000.00"),
        }
        if spec["shape"] == "item":
            row.update({"item_name": "Widget", "price": Decimal("1000.00"),
                        "quantity": Decimal("1")})
        else:
            row.update({"item_details": "Widget", "debit_or_credit": "debit"})
        spec["product"].objects.create(**row)
        return bill

    def move(self, bill, src, dst):
        return self.client.post(self.url, {
            "from": src, "to": dst, "bill_ids": [str(bill.id)],
        }, format="json")

    # -- the gap the client reported ------------------------------------
    def test_every_pair_of_vouchers_can_move(self):
        for src, dst in permutations(MODULES, 2):
            with self.subTest(frm=src, to=dst):
                bill = self.make_bill(src)
                response = self.move(bill, src, dst)
                self.assertEqual(
                    response.status_code, status.HTTP_200_OK, response.data,
                )
                self.assertEqual(
                    response.data["successful_moves"], 1,
                    f"{src} -> {dst} failed: {response.data.get('errors')}",
                )
                self.assertFalse(
                    MODULES[src]["bill"].objects.filter(id=bill.id).exists(),
                    f"{src} -> {dst} left the source bill behind",
                )

    def test_the_document_and_its_reading_come_across(self):
        bill = self.make_bill("receipt")
        self.move(bill, "receipt", "vendor")
        moved = MODULES["vendor"]["bill"].objects.get(organization=self.org)
        self.assertEqual(moved.analysed_data.get("invoice_number"), "MOVE-1")
        self.assertEqual(moved.status, "Analysed")

    def test_line_items_survive_the_translation(self):
        """A posting becomes a goods line and keeps its money."""
        bill = self.make_bill("payment")
        self.move(bill, "payment", "sales")
        analysed = MODULES["sales"]["analysed"].objects.get(organization=self.org)
        products = list(analysed.products.all())
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0].amount, Decimal("1000.00"))

    # -- what a move must NOT do ----------------------------------------
    def test_a_party_does_not_cross_sides(self):
        """Purchase and sales keep their party in different columns."""
        self.assertNotEqual(MODULES["vendor"]["party"], MODULES["sales"]["party"])
        bill = self.make_bill("vendor")
        self.move(bill, "vendor", "sales")
        analysed = MODULES["sales"]["analysed"].objects.get(organization=self.org)
        self.assertIsNone(
            analysed.customer,
            "a supplier ledger was written into the customer field",
        )

    def test_a_synced_bill_cannot_be_moved(self):
        bill = self.make_bill("vendor", status_value="Synced")
        response = self.move(bill, "vendor", "expense")
        self.assertEqual(response.data["successful_moves"], 0)
        self.assertTrue(
            MODULES["vendor"]["bill"].objects.filter(id=bill.id).exists(),
            "a synced bill was moved out from under Tally",
        )

    def test_an_unknown_voucher_is_refused(self):
        bill = self.make_bill("vendor")
        response = self.client.post(self.url, {
            "from": "vendor", "to": "nonsense", "bill_ids": [str(bill.id)],
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

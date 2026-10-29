"""What the Zoho schema now guarantees.

Two things used to go wrong quietly here, and both cost real work:

* Deleting one stale Zoho master destroyed the analysed data of every
  bill that referenced it. Thirty foreign keys reached the masters and
  all thirty cascaded, so an operator tidying up a ledger that Zoho no
  longer had could lose a month of analysed bills. Nothing warned them
  and nothing said so afterwards.

* Nothing stopped a master row being inserted twice, so two concurrent
  syncs could duplicate a vendor. From then on ``update_or_create``
  raised MultipleObjectsReturned and the sync stayed broken.
"""

from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.module.zoho.models import (
    VendorBill, VendorZohoBill, VendorZohoProduct, ZohoChartOfAccount,
    ZohoCredentials, ZohoCustomer, ZohoTaxes, ZohoTdsTcs, ZohoVendor,
)
from apps.organizations.models import Organization
from apps.users.models import User


class MasterDeletionTests(TestCase):
    """A stale master may be removed; the bills that named it stay."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            email="schema@example.com", password="x", first_name="S", last_name="C",
        )
        cls.org = Organization.objects.create(
            name="Schema Co", created_by=cls.user, owner=cls.user,
        )

    def _analysed_bill(self):
        bill = VendorBill.objects.create(
            organization=self.org, billmunshiName="SCHEMA-1",
        )
        vendor = ZohoVendor.objects.create(
            organization=self.org, contactId="c-1", companyName="Acme", gstNo="",
        )
        account = ZohoChartOfAccount.objects.create(
            organization=self.org, accountId="a-1", accountName="Purchases",
        )
        tax = ZohoTaxes.objects.create(
            organization=self.org, taxId="t-1", taxName="GST18",
        )
        analysed = VendorZohoBill.objects.create(
            organization=self.org, selectBill=bill, vendor=vendor,
        )
        line = VendorZohoProduct.objects.create(
            organization=self.org, zohoBill=analysed,
            chart_of_accounts=account, taxes=tax,
        )
        return bill, analysed, line, vendor, account, tax

    def test_deleting_a_vendor_keeps_the_bill_it_was_mapped_to(self):
        bill, analysed, _, vendor, _, _ = self._analysed_bill()

        vendor.delete()

        analysed.refresh_from_db()
        self.assertIsNone(analysed.vendor)
        # The bill and its analysis survive — only the mapping is gone,
        # which is the same state as a bill not yet mapped.
        self.assertTrue(VendorBill.objects.filter(pk=bill.pk).exists())
        self.assertTrue(VendorZohoBill.objects.filter(pk=analysed.pk).exists())

    def test_deleting_the_chart_of_accounts_keeps_the_lines(self):
        """This was the worst case: it took essentially every bill."""
        _, _, line, _, account, _ = self._analysed_bill()

        account.delete()

        line.refresh_from_db()
        self.assertIsNone(line.chart_of_accounts)
        self.assertTrue(VendorZohoProduct.objects.filter(pk=line.pk).exists())

    def test_deleting_a_tax_keeps_the_lines(self):
        _, _, line, _, _, tax = self._analysed_bill()

        tax.delete()

        line.refresh_from_db()
        self.assertIsNone(line.taxes)
        self.assertTrue(VendorZohoProduct.objects.filter(pk=line.pk).exists())

    def test_wiping_every_master_leaves_the_analysed_bills_standing(self):
        """The whole-masters reset an operator actually runs."""
        bill, analysed, line, _, _, _ = self._analysed_bill()

        ZohoVendor.objects.all().delete()
        ZohoChartOfAccount.objects.all().delete()
        ZohoTaxes.objects.all().delete()

        self.assertTrue(VendorBill.objects.filter(pk=bill.pk).exists())
        self.assertTrue(VendorZohoBill.objects.filter(pk=analysed.pk).exists())
        self.assertTrue(VendorZohoProduct.objects.filter(pk=line.pk).exists())

    # Deleting the workspace itself must still take its data with it.
    def test_deleting_the_organisation_still_removes_its_data(self):
        self._analysed_bill()
        other_user = User.objects.create_user(
            email="other@example.com", password="x", first_name="O", last_name="C",
        )
        doomed = Organization.objects.create(
            name="Doomed", created_by=other_user, owner=other_user,
        )
        ZohoVendor.objects.create(
            organization=doomed, contactId="c-9", companyName="Gone", gstNo="",
        )

        doomed.delete()

        self.assertFalse(ZohoVendor.objects.filter(organization_id=doomed.id).exists())
        self.assertEqual(ZohoVendor.objects.count(), 1)


class MasterUniquenessTests(TestCase):
    """One row per Zoho object per workspace, enforced by the database."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            email="uniq@example.com", password="x", first_name="U", last_name="Q",
        )
        cls.org = Organization.objects.create(
            name="Uniq Co", created_by=cls.user, owner=cls.user,
        )
        cls.other = Organization.objects.create(
            name="Other Co", created_by=cls.user, owner=cls.user,
        )

    def _duplicate_is_refused(self, model, **kwargs):
        model.objects.create(organization=self.org, **kwargs)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                model.objects.create(organization=self.org, **kwargs)

    def test_a_vendor_cannot_be_inserted_twice(self):
        self._duplicate_is_refused(
            ZohoVendor, contactId="c-1", companyName="Acme", gstNo="",
        )

    def test_a_customer_cannot_be_inserted_twice(self):
        self._duplicate_is_refused(
            ZohoCustomer, contactId="c-1", companyName="Acme", gstNo="",
        )

    def test_an_account_cannot_be_inserted_twice(self):
        self._duplicate_is_refused(
            ZohoChartOfAccount, accountId="a-1", accountName="Purchases",
        )

    def test_a_tax_cannot_be_inserted_twice(self):
        self._duplicate_is_refused(ZohoTaxes, taxId="t-1", taxName="GST18")

    def test_a_workspace_cannot_hold_two_credential_rows(self):
        """``get_or_create(organization=...)`` breaks outright if it can."""
        ZohoCredentials.objects.create(organization=self.org, clientId="a")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ZohoCredentials.objects.create(organization=self.org, clientId="b")

    # -- what must still be allowed ----------------------------------

    def test_two_workspaces_may_hold_the_same_zoho_id(self):
        """Uniqueness is per workspace, never global."""
        ZohoVendor.objects.create(
            organization=self.org, contactId="shared", companyName="A", gstNo="",
        )
        ZohoVendor.objects.create(
            organization=self.other, contactId="shared", companyName="A", gstNo="",
        )

        self.assertEqual(ZohoVendor.objects.filter(contactId="shared").count(), 2)

    def test_tds_and_tcs_may_share_a_tax_id(self):
        """Zoho reuses an id across the two lists; both rows are wanted."""
        ZohoTdsTcs.objects.create(
            organization=self.org, taxId="t-1", taxName="194C", taxType="TDS",
        )
        ZohoTdsTcs.objects.create(
            organization=self.org, taxId="t-1", taxName="194C", taxType="TCS",
        )

        self.assertEqual(ZohoTdsTcs.objects.count(), 2)

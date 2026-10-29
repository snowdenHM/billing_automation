"""Clear a workspace's Zoho data so it can be connected again from scratch.

Reconnecting to a different Zoho organisation leaves the previous one's
masters behind — a chart of accounts, tax codes and vendor list that
belong to books this workspace can no longer reach. Those stale rows
then get offered on bill screens and quietly picked, so a voucher is
built against an account that does not exist in the new org.

    python manage.py reset_zoho_connection --organization <uuid> --dry-run
    python manage.py reset_zoho_connection --organization <uuid>

**Read this before running it.** Clearing the masters does NOT leave the
bills alone, however much it looks like it should. Twenty-nine foreign
keys point from analysed Zoho bill data at the masters — every line's
chart-of-accounts and tax, every bill's vendor or customer — and all of
them are ``on_delete=CASCADE``. Deleting one vendor takes every analysed
bill that named it; deleting the chart of accounts takes essentially all
of them.

So "masters only" removes the *uploaded documents* but destroys their
*analysed data*, leaving bills that have to be analysed again.
``--include-bills`` removes the uploaded documents as well.

That cascade is worth fixing on its own — an operator tidying up one
stale ledger should not silently lose a month of analysed bills — but it
is a schema change across 29 relations and does not belong in a cleanup
command.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Clear Zoho masters (and optionally bills) for one organisation."

    def add_arguments(self, parser):
        parser.add_argument(
            "--organization",
            help="Organisation id. Omit to act on every organisation.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be removed without removing anything.",
        )
        parser.add_argument(
            "--include-bills", action="store_true",
            help=(
                "Also delete the uploaded Zoho bill documents. Their "
                "analysed data goes either way — it cascades off the "
                "masters. Nothing here is recoverable."
            ),
        )
        parser.add_argument(
            "--keep-credentials", action="store_true",
            help=(
                "Keep the OAuth credentials row. Use when re-syncing the "
                "same organisation rather than connecting a different one."
            ),
        )

    def handle(self, *args, **options):
        from apps.module.zoho.models import (
            ZohoChartOfAccount, ZohoCredentials, ZohoCustomer, ZohoTaxes,
            ZohoTdsTcs, ZohoVendor, ZohoVendorCredit,
        )
        from apps.organizations.models import Organization

        dry = options["dry_run"]
        org_id = options["organization"]

        orgs = Organization.objects.all()
        if org_id:
            orgs = orgs.filter(id=org_id)
            if not orgs.exists():
                raise CommandError(f"No organisation with id {org_id}")

        if dry:
            self.stdout.write(self.style.WARNING("DRY RUN — nothing will be deleted"))

        masters = [
            ("Chart of accounts", ZohoChartOfAccount),
            ("Tax codes", ZohoTaxes),
            ("TDS / TCS", ZohoTdsTcs),
            ("Vendors", ZohoVendor),
            ("Customers", ZohoCustomer),
            ("Vendor credits", ZohoVendorCredit),
        ]

        bill_models = []
        if options["include_bills"]:
            from apps.module.zoho import models as zm
            # Children before parents: the FKs cascade, but deleting in
            # this order keeps the per-model counts honest in the report.
            for name in (
                "VendorZohoProduct", "VendorZohoConsolidatedProduct",
                "SalesZohoProduct", "SalesZohoConsolidatedProduct",
                "ExpenseZohoProduct", "ExpenseZohoConsolidatedProduct",
                "JournalZohoProduct", "JournalZohoConsolidatedProduct",
                "VendorZohoBill", "SalesZohoBill", "ExpenseZohoBill",
                "JournalZohoBill", "PaymentZohoBill", "ReceiptZohoBill",
                "VendorBill", "SalesBill", "ExpenseBill", "JournalBill",
                "PaymentBill", "ReceiptBill",
            ):
                model = getattr(zm, name, None)
                if model is not None:
                    bill_models.append((name, model))

        for org in orgs:
            self.stdout.write(f"\n{org.name} ({org.id})")
            removed = 0

            for label, model in masters:
                qs = model.objects.filter(organization=org)
                count = qs.count()
                if not count:
                    continue
                self.stdout.write(f"    {label:22} {count}")
                removed += count
                if not dry:
                    qs.delete()

            if not options["keep_credentials"]:
                qs = ZohoCredentials.objects.filter(organization=org)
                count = qs.count()
                if count:
                    self.stdout.write(f"    {'OAuth credentials':22} {count}")
                    removed += count
                    if not dry:
                        qs.delete()

            if bill_models:
                self.stdout.write(self.style.WARNING("    --- bills (not recoverable) ---"))
                for name, model in bill_models:
                    # `objects` may be a soft-delete manager; go wide so
                    # trashed rows are cleared too, since the point is a
                    # clean slate.
                    manager = getattr(model, "all_objects", model.objects)
                    qs = manager.filter(organization=org)
                    count = qs.count()
                    if not count:
                        continue
                    self.stdout.write(f"    {name:26} {count}")
                    removed += count
                    if not dry:
                        qs.delete()

                from apps.common.models import BillActivity
                qs = BillActivity.objects.filter(organization=org, module="zoho")
                count = qs.count()
                if count:
                    self.stdout.write(f"    {'activity log rows':26} {count}")
                    removed += count
                    if not dry:
                        qs.delete()

            if removed and not bill_models:
                self.stdout.write(self.style.WARNING(
                    "    note: analysed bill data cascades off these masters "
                    "and has gone too. The uploaded documents remain and will "
                    "need analysing again."
                ))

            if not removed:
                self.stdout.write("    nothing to remove")
            else:
                self.stdout.write(
                    f"    {'would remove' if dry else 'removed'}: {removed} row(s)"
                )

        if dry:
            self.stdout.write(self.style.WARNING("\nDRY RUN — nothing was deleted"))
        else:
            self.stdout.write(self.style.SUCCESS(
                "\nDone. Reconnect from Config -> Credentials, then run the "
                "syncs — Chart of accounts, Taxes, TDS/TCS, Vendors, Customers."
            ))

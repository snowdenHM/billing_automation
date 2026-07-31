"""Rebuild payment-voucher line items that analysis dropped on the floor.

``payment_bills.py`` used to read AI line items from ``payments`` while the
prompt emits them under ``expenses``. Analysis therefore "succeeded" — header
totals, GST and dates all landed — but every voucher was saved with zero
``TallyPaymentAnalyzedProduct`` rows, so the Credit/Debit table opened empty.

The reader is fixed, but that only helps bills analysed from now on: the
analyze endpoint short-circuits on ``bill.process`` and the existing-data path
returns early once a ``TallyPaymentAnalyzedBill`` row exists. This command
re-derives the missing rows from the ``analysed_data`` already stored on each
bill — no re-OCR, no OpenAI spend.

Only vouchers with **zero** product rows are touched, so a voucher an operator
has already filled in by hand is never overwritten. Run ``--dry-run`` first.

    python manage.py backfill_payment_line_items --dry-run
    python manage.py backfill_payment_line_items
    python manage.py backfill_payment_line_items --org <uuid>
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.module.tally.models import (
    TallyPaymentAnalyzedBill,
    TallyPaymentAnalyzedProduct,
    TallyPaymentConsolidatedProduct,
)
from apps.module.tally.views.payment_bills import (
    extract_payment_bill_number,
    extract_payment_line_items,
    find_appropriate_payment_tax_ledger,
    find_or_create_payment_chart_of_accounts_ledger,
)
from apps.common.converters import to_decimal


class Command(BaseCommand):
    help = "Rebuild payment-voucher line items lost to the expenses/payments key mismatch"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would change without writing anything.",
        )
        parser.add_argument(
            "--org", dest="org_id", default=None,
            help="Limit to a single organization UUID.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        org_id = options["org_id"]

        analyzed = (
            TallyPaymentAnalyzedBill.objects
            .select_related("selected_bill", "organization")
            .filter(products__isnull=True)          # zero line items
            .distinct()
        )
        if org_id:
            analyzed = analyzed.filter(organization_id=org_id)

        repaired = skipped_no_data = skipped_no_items = 0

        for ab in analyzed:
            bill = ab.selected_bill
            data = getattr(bill, "analysed_data", None)
            if not isinstance(data, dict) or not data:
                skipped_no_data += 1
                continue

            items = extract_payment_line_items(data)
            if not items:
                # Genuinely itemless bill (or a payload we can't read) —
                # leave it for an operator rather than inventing rows.
                skipped_no_items += 1
                self.stdout.write(
                    f"  skip  {bill.bill_munshi_name}: no line items in "
                    f"analysed_data (keys: {sorted(data.keys())})"
                )
                continue

            self.stdout.write(
                f"  fix   {bill.bill_munshi_name}: {len(items)} line item(s)"
            )
            if dry_run:
                repaired += 1
                continue

            with transaction.atomic():
                self._rebuild(ab, items, data)
            repaired += 1

        verb = "would repair" if dry_run else "repaired"
        self.stdout.write(self.style.SUCCESS(
            f"\n{verb} {repaired} voucher(s); "
            f"skipped {skipped_no_data} with no analysed_data, "
            f"{skipped_no_items} with no extractable line items."
        ))
        if dry_run and repaired:
            self.stdout.write("Re-run without --dry-run to apply.")

    def _rebuild(self, analyzed_bill, items, data):
        """Recreate product rows for one analysed bill.

        Mirrors ``process_payment_analysis_data`` so a backfilled voucher is
        indistinguishable from a freshly analysed one.
        """
        organization = analyzed_bill.organization
        fallback_coa = find_appropriate_payment_tax_ledger(
            organization, 0, 0, 0, ledger_type="payment_coa",
        )

        products = []
        for item in items:
            if not isinstance(item, dict):
                continue
            category = str(item.get("category", "General Payments"))
            coa = find_or_create_payment_chart_of_accounts_ledger(category, organization)
            products.append(TallyPaymentAnalyzedProduct(
                payment_bill=analyzed_bill,
                item_details=str(item.get("description", "")),
                chart_of_accounts=coa or fallback_coa,
                amount=to_decimal(item.get("amount", 0)),
                debit_or_credit=TallyPaymentAnalyzedProduct.DebitCredit.DEBIT,
                organization=organization,
            ))

        if not products:
            return
        TallyPaymentAnalyzedProduct.objects.bulk_create(products)

        # The consolidated view is what the detail page opens by default,
        # so a backfill that skipped it would still look empty.
        if len(products) > 1 and not analyzed_bill.consolidated_products.exists():
            total = sum(p.amount for p in products)
            lines = "\n".join(
                f"• {p.item_details} (Amount: ₹{p.amount})" for p in products
            )
            TallyPaymentConsolidatedProduct.objects.create(
                payment_bill=analyzed_bill,
                organization=organization,
                item_details=f"Consolidated {len(products)} payment entries:\n{lines}",
                chart_of_accounts=fallback_coa,
                amount=total,
                debit_or_credit=TallyPaymentConsolidatedProduct.DebitCredit.DEBIT,
                original_entries_count=len(products),
                consolidation_notes=f"Backfilled for {len(products)} payment entries",
            )

        # Header fields the broken reader also missed.
        updates = []
        if not analyzed_bill.bill_no:
            recovered = extract_payment_bill_number(data)
            if recovered:
                analyzed_bill.bill_no = recovered
                updates.append("bill_no")
        if updates:
            analyzed_bill.save(update_fields=updates)

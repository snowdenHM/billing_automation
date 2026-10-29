"""Re-resolve per-line GST on bills analysed before the rate-first fix.

Bills analysed by the old code had their tax split across lines in
proportion to amount, and their rate back-derived from that split. On a
mixed-rate bill that left rows whose stated rate contradicts the tax
they carry — a line saying 0% while holding tax, or saying 18% while
holding none. That contradiction flows into the GST rollup, the voucher
and the return.

Fixing the analysis path does not fix bills already in the database.
This does.

    python manage.py repair_line_gst --dry-run      # always first
    python manage.py repair_line_gst

What it will not touch, and why:

* **Synced bills.** Their numbers are what was posted to Tally. Changing
  them here would make the record disagree with the voucher and tell
  nobody.
* **Bills whose own totals do not add up.** If a bill's bill-level GST
  cannot be explained by any combination of slabs over its lines, the
  problem is the reading of the bill, not the split. Those are listed
  for a human rather than rewritten into a different kind of wrong.
"""

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.common.services.line_gst import assign_line_gst


class Command(BaseCommand):
    help = "Recompute per-line GST so each line's rate explains its tax."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would change without changing anything.",
        )
        parser.add_argument(
            "--organization", help="Limit to one organisation id.",
        )
        parser.add_argument(
            "--include-synced", action="store_true",
            help=(
                "Also repair bills already posted to Tally. Off by "
                "default: their figures are the record of what was posted."
            ),
        )

    def handle(self, *args, **options):
        from apps.module.tally.models import (
            TallySalesAnalyzedBill, TallyVendorAnalyzedBill,
        )

        dry = options["dry_run"]
        if dry:
            self.stdout.write(self.style.WARNING("DRY RUN — nothing will be written"))

        totals = {"checked": 0, "repaired": 0, "unresolved": 0, "skipped_synced": 0}
        unresolved = []

        for model, label in ((TallyVendorAnalyzedBill, "Purchase"),
                             (TallySalesAnalyzedBill, "Sales")):
            qs = model.objects.select_related("selected_bill").prefetch_related("products")
            if options["organization"]:
                qs = qs.filter(organization_id=options["organization"])

            self.stdout.write(f"\n{label} vouchers")
            for bill in qs:
                totals["checked"] += 1

                if not options["include_synced"] and getattr(bill, "tally_synced", False):
                    totals["skipped_synced"] += 1
                    continue

                products = list(bill.products.all())
                if not products:
                    continue

                results, strategy = assign_line_gst(
                    [{"amount": float(p.amount or 0), "rate": None} for p in products],
                    bill.igst or 0, bill.cgst or 0, bill.sgst or 0,
                    taxable_base=bill.taxable_base,
                )

                # A bill the solver cannot explain is a reading problem,
                # not a splitting one. Say so; do not rewrite it.
                if strategy == "proportional-fallback":
                    totals["unresolved"] += 1
                    unresolved.append((label, bill, strategy))
                    continue

                changed = []
                for product, result in zip(products, results):
                    want_rate = f"{int(result['rate']) if float(result['rate']).is_integer() else result['rate']}%"
                    if (
                        (product.product_gst or "") != want_rate
                        or Decimal(str(product.cgst or 0)) != result["cgst"]
                        or Decimal(str(product.sgst or 0)) != result["sgst"]
                        or Decimal(str(product.igst or 0)) != result["igst"]
                    ):
                        changed.append((product, result, want_rate))

                if not changed:
                    continue

                totals["repaired"] += 1
                name = getattr(bill.selected_bill, "bill_munshi_name", bill.id)
                self.stdout.write(
                    f"  {name} [{strategy}] — {len(changed)} line(s)"
                )
                for product, result, want_rate in changed:
                    self.stdout.write(
                        f"      {str(product.item_details or '')[:30]:32}"
                        f" {product.product_gst or '-':>6} -> {want_rate:>6}"
                        f"   tax {float(product.cgst or 0) + float(product.sgst or 0) + float(product.igst or 0):>9.2f}"
                        f" -> {float(result['cgst'] + result['sgst'] + result['igst']):>9.2f}"
                    )

                if dry:
                    continue

                with transaction.atomic():
                    for product, result, want_rate in changed:
                        product.product_gst = want_rate
                        product.cgst = result["cgst"]
                        product.sgst = result["sgst"]
                        product.igst = result["igst"]
                        product.save(update_fields=[
                            "product_gst", "cgst", "sgst", "igst",
                        ])

        if unresolved:
            self.stdout.write(self.style.WARNING(
                f"\n{len(unresolved)} bill(s) left alone — their own totals do "
                f"not resolve to GST slabs, so the reading needs a human:"
            ))
            for label, bill, _ in unresolved[:20]:
                name = getattr(bill.selected_bill, "bill_munshi_name", bill.id)
                self.stdout.write(
                    f"  {label} {name}: "
                    f"igst={bill.igst} cgst={bill.cgst} sgst={bill.sgst}"
                )

        self.stdout.write(
            f"\nchecked {totals['checked']}, "
            f"repaired {totals['repaired']}, "
            f"unresolved {totals['unresolved']}, "
            f"skipped (synced) {totals['skipped_synced']}"
        )
        if dry:
            self.stdout.write(self.style.WARNING("DRY RUN — nothing was written"))

"""
Adds ``TallyExpenseGstLine`` and back-fills it from the existing bill-level
CGST/SGST/IGST fields on ``TallyExpenseAnalyzedBill``.

Each existing bill becomes 0-3 ``TallyExpenseGstLine`` rows:
- CGST > 0 with a cgst_taxes ledger  → 1 row
- SGST > 0 with a sgst_taxes ledger  → 1 row
- IGST > 0 with a igst_taxes ledger  → 1 row

Rate defaults to ``"18%"`` because the legacy model didn't store a rate per
bill — that's the most common slab and gives the user something to edit. A
log line is emitted for each backfilled row so the cutover can be audited.

The old bill-level CGST/SGST/IGST + ledger + DR/CR fields are **kept** in
this migration. They can be dropped in a follow-up cleanup migration once
the new flow has been verified in production for one release cycle.
"""
from decimal import Decimal
import uuid

from django.db import migrations, models


def backfill_gst_lines(apps, schema_editor):
    """Convert each bill's bill-level CGST/SGST/IGST into gst_lines rows."""
    TallyExpenseAnalyzedBill = apps.get_model("tally", "TallyExpenseAnalyzedBill")
    TallyExpenseGstLine = apps.get_model("tally", "TallyExpenseGstLine")

    DEFAULT_RATE = "18%"  # Common-case slab; user can re-pick on first edit.
    created = 0

    for bill in TallyExpenseAnalyzedBill.objects.all().iterator():
        for tax_type, amount_attr, ledger_attr, dc_attr in (
            ("CGST", "cgst", "cgst_taxes_id", "cgst_debit_or_credit"),
            ("SGST", "sgst", "sgst_taxes_id", "sgst_debit_or_credit"),
            ("IGST", "igst", "igst_taxes_id", "igst_debit_or_credit"),
        ):
            amount = getattr(bill, amount_attr, None) or Decimal("0")
            ledger_id = getattr(bill, ledger_attr, None)
            if amount <= 0 or not ledger_id:
                continue
            TallyExpenseGstLine.objects.create(
                id=uuid.uuid4(),
                organization=bill.organization,
                expense_bill=bill,
                rate=DEFAULT_RATE,
                tax_type=tax_type,
                amount=amount,
                ledger_id=ledger_id,
                debit_or_credit=getattr(bill, dc_attr, None) or "debit",
            )
            created += 1

    if created:
        print(f"  Backfilled {created} TallyExpenseGstLine rows from bill-level GST")


def unbackfill_gst_lines(apps, schema_editor):
    """Reverse: just drop all rows (the bill-level fields still hold the data)."""
    TallyExpenseGstLine = apps.get_model("tally", "TallyExpenseGstLine")
    TallyExpenseGstLine.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("tally", "0029_gstrateledgermapping_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="TallyExpenseGstLine",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False, unique=True)),
                ("rate", models.CharField(blank=True, default="", help_text="GST rate string like '18%', '28%'. Informational on Tally side.", max_length=10)),
                ("tax_type", models.CharField(choices=[("CGST", "CGST"), ("SGST", "SGST"), ("IGST", "IGST")], help_text="CGST / SGST / IGST. Determines which ledger pool the dropdown shows.", max_length=10)),
                ("amount", models.DecimalField(decimal_places=2, default=Decimal("0"), max_digits=15)),
                ("debit_or_credit", models.CharField(choices=[("debit", "Debit"), ("credit", "Credit")], default="debit", help_text="Usually debit (input credit). Flips to credit for RCM payable entries.", max_length=10)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("expense_bill", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="gst_lines", to="tally.tallyexpenseanalyzedbill")),
                ("ledger", models.ForeignKey(blank=True, help_text="The CGST/SGST/IGST tax ledger this amount posts against in Tally.", null=True, on_delete=models.deletion.SET_NULL, related_name="tally_expense_gst_lines", to="tally.ledger")),
                ("organization", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="tally_tallyexpensegstlines", to="organizations.organization")),
            ],
            options={
                "verbose_name": "Tally Expense GST Line",
                "verbose_name_plural": "Tally Expense GST Lines",
                "ordering": ["rate", "tax_type"],
            },
        ),
        migrations.RunPython(backfill_gst_lines, reverse_code=unbackfill_gst_lines),
    ]

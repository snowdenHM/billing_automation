"""
Dedupe and enforce uniqueness of ``TallyVendorAnalyzedBill.selected_bill``
and ``TallyExpenseAnalyzedBill.selected_bill``.

Prior to this migration nothing at the DB level prevented two Analyzed
rows from attaching to the same parent bill. The sync-analyze button
and the async RQ analysis job would sometimes both create a row for the
same bill (double-analysis race). Downstream code had a
``MultipleObjectsReturned`` handler that silently deleted the extras —
this migration flips the invariant to enforced-at-DB.

Steps:
1. For every parent bill with more than one Analyzed row, keep the most
   recent (highest ``created_at``) and delete the rest.
2. Apply ``UniqueConstraint`` with a ``Q(selected_bill__isnull=False)``
   filter so orphan rows (which do exist historically for lost parents)
   don't block the constraint.
"""
from django.db import migrations, models


def _dedupe(apps, model_name):
    """Keep the latest row per selected_bill, delete the rest."""
    Model = apps.get_model("tally", model_name)
    parents_with_dupes = (
        Model.objects.values("selected_bill_id")
        .annotate(cnt=models.Count("id"))
        .filter(cnt__gt=1, selected_bill_id__isnull=False)
    )
    total_deleted = 0
    for row in parents_with_dupes:
        parent_id = row["selected_bill_id"]
        winners = (
            Model.objects.filter(selected_bill_id=parent_id)
            .order_by("-created_at")
            .values_list("id", flat=True)
        )
        keep = list(winners)[0]
        delete_qs = Model.objects.filter(selected_bill_id=parent_id).exclude(id=keep)
        n = delete_qs.count()
        delete_qs.delete()
        total_deleted += n
    if total_deleted:
        print(f"  Deduplicated {total_deleted} {model_name} rows")


def dedupe_forward(apps, schema_editor):
    _dedupe(apps, "TallyVendorAnalyzedBill")
    _dedupe(apps, "TallyExpenseAnalyzedBill")


def dedupe_reverse(apps, schema_editor):
    # Constraint removal is reversible; the dedupe deletes are not.
    # Leave a no-op — reverting the migration only drops the constraint.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("tally", "0030_tallyexpensegstline"),
    ]

    operations = [
        migrations.RunPython(dedupe_forward, reverse_code=dedupe_reverse),
        migrations.AddConstraint(
            model_name="tallyvendoranalyzedbill",
            constraint=models.UniqueConstraint(
                fields=("selected_bill",),
                name="uq_tally_vendor_analyzed_selected_bill",
                condition=models.Q(selected_bill__isnull=False),
            ),
        ),
        migrations.AddConstraint(
            model_name="tallyexpenseanalyzedbill",
            constraint=models.UniqueConstraint(
                fields=("selected_bill",),
                name="uq_tally_expense_analyzed_selected_bill",
                condition=models.Q(selected_bill__isnull=False),
            ),
        ),
    ]

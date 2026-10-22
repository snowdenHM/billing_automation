# Generated manually.
"""Append-only activity log for every bill, in every module.

Nothing recorded who changed what on a bill. The log addresses its bill by
(module, bill_type, bill_id) instead of a ForeignKey because the six bill
models live in two apps and the log has to outlive the row it describes.
"""

import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("common", "0004_emailsettings_provider_smtp"),
        ("organizations", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="BillActivity",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("module", models.CharField(choices=[("tally", "Tally"), ("zoho", "Zoho")], max_length=16)),
                ("bill_type", models.CharField(choices=[("vendor", "Purchase Voucher"), ("expense", "Journal Entry"), ("payment", "Payment Voucher"), ("journal", "Journal")], max_length=16)),
                ("bill_id", models.UUIDField()),
                ("action", models.CharField(choices=[("uploaded", "Uploaded"), ("analysis_queued", "Analysis queued"), ("analysis_completed", "Analysis completed"), ("analysis_failed", "Analysis failed"), ("duplicate_flagged", "Flagged as duplicate"), ("verified", "Verified"), ("sync_requested", "Sent to accounting software"), ("sync_confirmed", "Posted"), ("sync_failed", "Posting failed"), ("moved", "Moved"), ("trashed", "Moved to Trash"), ("restored", "Restored from Trash"), ("deleted", "Deleted permanently")], max_length=32)),
                ("actor_label", models.CharField(blank=True, default="", max_length=255)),
                ("summary", models.CharField(blank=True, default="", max_length=255)),
                ("changes", models.JSONField(blank=True, default=list)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("actor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="bill_activities", to=settings.AUTH_USER_MODEL)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="bill_activities", to="organizations.organization")),
            ],
            options={
                "verbose_name": "Bill Activity",
                "verbose_name_plural": "Bill Activity",
                "ordering": ("-created_at", "-id"),
            },
        ),
        migrations.AddIndex(
            model_name="billactivity",
            index=models.Index(fields=["bill_id", "-created_at"], name="billactivity_bill_idx"),
        ),
        migrations.AddIndex(
            model_name="billactivity",
            index=models.Index(fields=["organization", "-created_at"], name="billactivity_org_idx"),
        ),
        migrations.AddIndex(
            model_name="billactivity",
            index=models.Index(fields=["organization", "action", "-created_at"], name="billactivity_org_action_idx"),
        ),
        migrations.AddIndex(
            model_name="billactivity",
            index=models.Index(fields=["organization", "module", "bill_type", "-created_at"], name="billactivity_learn_idx"),
        ),
    ]

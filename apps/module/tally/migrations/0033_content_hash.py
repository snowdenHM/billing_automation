"""
Add ``content_hash`` SHA-256 field to Tally bill models for exact-duplicate
detection at upload time (see #8 in the upload audit).
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tally", "0032_per_org_upload_path"),
    ]

    operations = [
        migrations.AddField(
            model_name="tallyvendorbill",
            name="content_hash",
            field=models.CharField(
                max_length=64, blank=True, null=True, db_index=True,
                help_text="SHA-256 hex digest of the uploaded file for exact-duplicate detection",
            ),
        ),
        migrations.AddField(
            model_name="tallyexpensebill",
            name="content_hash",
            field=models.CharField(
                max_length=64, blank=True, null=True, db_index=True,
                help_text="SHA-256 hex digest of the uploaded file for exact-duplicate detection",
            ),
        ),
    ]

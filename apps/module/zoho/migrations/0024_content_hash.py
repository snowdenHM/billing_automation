"""
Add ``content_hash`` SHA-256 field to Zoho bill models for exact-duplicate
detection at upload time (see #8 in the upload audit).
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("zoho", "0023_per_org_upload_path"),
    ]

    operations = [
        migrations.AddField(
            model_name="vendorbill",
            name="content_hash",
            field=models.CharField(
                max_length=64, blank=True, null=True, db_index=True,
                help_text="SHA-256 hex digest of the uploaded file for exact-duplicate detection",
            ),
        ),
        migrations.AddField(
            model_name="expensebill",
            name="content_hash",
            field=models.CharField(
                max_length=64, blank=True, null=True, db_index=True,
                help_text="SHA-256 hex digest of the uploaded file for exact-duplicate detection",
            ),
        ),
        migrations.AddField(
            model_name="journalbill",
            name="content_hash",
            field=models.CharField(
                max_length=64, blank=True, null=True, db_index=True,
                help_text="SHA-256 hex digest of the uploaded file for exact-duplicate detection",
            ),
        ),
    ]

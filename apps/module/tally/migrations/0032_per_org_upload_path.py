"""
Switch bill FileField ``upload_to`` from the flat ``"bills/"`` string to a
callable that stamps the org id in the path (``bills/<org_id>/<filename>``).

Existing files stay where they are — Django doesn't rewrite already-saved
FileField values. Only new uploads land under the per-org subdirectory.
"""
from django.db import migrations, models

import apps.common.validators


class Migration(migrations.Migration):

    dependencies = [
        ("tally", "0031_analyzed_bill_unique"),
    ]

    operations = [
        migrations.AlterField(
            model_name="tallyvendorbill",
            name="file",
            field=models.FileField(
                upload_to=apps.common.validators.bill_upload_path,
                validators=[
                    apps.common.validators.validate_file_extension,
                    apps.common.validators.validate_bill_file_size,
                ],
            ),
        ),
        migrations.AlterField(
            model_name="tallyexpensebill",
            name="file",
            field=models.FileField(
                upload_to=apps.common.validators.bill_upload_path,
                validators=[
                    apps.common.validators.validate_file_extension,
                    apps.common.validators.validate_bill_file_size,
                ],
            ),
        ),
    ]

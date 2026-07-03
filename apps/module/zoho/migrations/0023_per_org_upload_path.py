"""
Same as the Tally-side per-org upload path migration — switch bill
FileField ``upload_to`` to a callable so new uploads land under
``bills/<org_id>/``. Existing files stay in place.
"""
from django.db import migrations, models

import apps.common.validators


class Migration(migrations.Migration):

    dependencies = [
        ("zoho", "0022_expensebill_bill_belong_your_org_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="vendorbill",
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
            model_name="expensebill",
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
            model_name="journalbill",
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

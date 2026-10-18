from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("zoho", "0025_alter_expensebill_process_alter_journalbill_process_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="vendorbill",
            name="zoho_bill_id",
            field=models.CharField(
                max_length=64,
                blank=True,
                null=True,
                db_index=True,
                help_text=(
                    "bill_id returned by Zoho Books. Set once the remote bill "
                    "exists; blocks a re-sync from creating a duplicate."
                ),
            ),
        ),
    ]

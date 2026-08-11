# Generated manually (no local Django env available) — mirrors 0026_tallyconfig_round_off_parents_and_more.py

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tally", "0037_tallyexpensebill_deleted_at_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="tallyconfig",
            name="cess_parents",
            field=models.ManyToManyField(
                blank=True,
                db_table="tally_config_cess_parents",
                help_text="Parent ledgers (typically 'Indirect Expenses') used to source the Cess ledger.",
                related_name="cess_tally_configs",
                to="tally.parentledger",
                verbose_name="Cess Parent Ledgers",
            ),
        ),
        migrations.AddField(
            model_name="tallyconfig",
            name="discount_parents",
            field=models.ManyToManyField(
                blank=True,
                db_table="tally_config_discount_parents",
                help_text="Parent ledgers (typically 'Indirect Expenses') used to source the Discount ledger.",
                related_name="discount_tally_configs",
                to="tally.parentledger",
                verbose_name="Discount Parent Ledgers",
            ),
        ),
        migrations.AddField(
            model_name="tallyconfig",
            name="freight_parents",
            field=models.ManyToManyField(
                blank=True,
                db_table="tally_config_freight_parents",
                help_text="Parent ledgers (typically 'Indirect Expenses') used to source the Freight Charges ledger.",
                related_name="freight_tally_configs",
                to="tally.parentledger",
                verbose_name="Freight Charges Parent Ledgers",
            ),
        ),
    ]

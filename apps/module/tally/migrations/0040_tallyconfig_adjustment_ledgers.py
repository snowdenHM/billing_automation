# Generated manually (no local Django env available for `makemigrations`).
"""Client request: adjustment mapping picks a specific Ledger, not a Parent.

Adds direct FK fields on TallyConfig for the five Additional Adjustments
so a bill's Cess / Discount / Freight / Round Off / TDS ledger can be
selected from the org's full Chart of Accounts (every Ledger row),
instead of picking a ParentLedger and letting the backend guess a
child under it.

The existing ``*_parents`` M2M columns remain in place — they still
inform backfill / import defaults and the auto-select behaviour of the
verify screen.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tally', '0039_tallypaymentanalyzedbill_payment_mode'),
    ]

    operations = [
        migrations.AddField(
            model_name='tallyconfig',
            name='cess_ledger',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='cess_tally_configs',
                to='tally.ledger',
                verbose_name='Cess ledger',
                help_text='Specific ledger applied when a bill has Cess.',
            ),
        ),
        migrations.AddField(
            model_name='tallyconfig',
            name='discount_ledger',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='discount_tally_configs',
                to='tally.ledger',
                verbose_name='Discount ledger',
                help_text='Specific ledger applied when a bill has Discount.',
            ),
        ),
        migrations.AddField(
            model_name='tallyconfig',
            name='freight_ledger',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='freight_tally_configs_direct',
                to='tally.ledger',
                verbose_name='Freight ledger',
                help_text='Specific ledger applied when a bill has Freight Charges.',
            ),
        ),
        migrations.AddField(
            model_name='tallyconfig',
            name='round_off_ledger',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='round_off_tally_configs',
                to='tally.ledger',
                verbose_name='Round Off ledger',
                help_text='Specific ledger applied for Round Off entries.',
            ),
        ),
        migrations.AddField(
            model_name='tallyconfig',
            name='tds_ledger',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='tds_tally_configs',
                to='tally.ledger',
                verbose_name='TDS ledger',
                help_text='Specific ledger applied when TDS is deducted.',
            ),
        ),
    ]

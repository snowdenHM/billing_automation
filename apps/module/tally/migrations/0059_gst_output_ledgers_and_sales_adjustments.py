"""Input/output GST ledgers, and a sales-side ledger per adjustment.

Written by hand rather than generated, because the toolchain only
exists inside the container. Every field here is a nullable FK with
``SET_NULL``, so the operation is an ``ADD COLUMN ... NULL`` per
column and nothing is rewritten; the existing columns are untouched
and keep their meaning (input tax, purchase-side adjustment).

``makemigrations --check`` on deploy will confirm the models and this
file agree.
"""

from django.db import migrations, models
import django.db.models.deletion


def _fk(related_name, help_text, verbose_name=None):
    kwargs = dict(
        blank=True,
        null=True,
        on_delete=django.db.models.deletion.SET_NULL,
        related_name=related_name,
        help_text=help_text,
        to='tally.ledger',
    )
    if verbose_name is not None:
        kwargs['verbose_name'] = verbose_name
    return models.ForeignKey(**kwargs)


class Migration(migrations.Migration):

    dependencies = [
        ('tally', '0058_alter_tallysalesanalyzedproduct_quantity_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='gstrateledgermapping',
            name='cgst_output_ledger',
            field=_fk(
                'cgst_output_rate_mappings',
                'CGST ledger for sales (output tax) at this rate.',
            ),
        ),
        migrations.AddField(
            model_name='gstrateledgermapping',
            name='sgst_output_ledger',
            field=_fk(
                'sgst_output_rate_mappings',
                'SGST ledger for sales (output tax) at this rate.',
            ),
        ),
        migrations.AddField(
            model_name='gstrateledgermapping',
            name='igst_output_ledger',
            field=_fk(
                'igst_output_rate_mappings',
                'IGST ledger for sales (output tax) at this rate.',
            ),
        ),
        migrations.AddField(
            model_name='tallyconfig',
            name='cess_ledger_sales',
            field=_fk('cess_sales_tally_configs', 'Ledger applied when a SALES voucher has Cess.', 'Cess ledger (sales)'),
        ),
        migrations.AddField(
            model_name='tallyconfig',
            name='discount_ledger_sales',
            field=_fk('discount_sales_tally_configs', 'Ledger applied when a SALES voucher has a Discount.', 'Discount ledger (sales)'),
        ),
        migrations.AddField(
            model_name='tallyconfig',
            name='freight_ledger_sales',
            field=_fk('freight_sales_tally_configs', 'Ledger applied when a SALES voucher has Freight Charges.', 'Freight ledger (sales)'),
        ),
        migrations.AddField(
            model_name='tallyconfig',
            name='round_off_ledger_sales',
            field=_fk('round_off_sales_tally_configs', 'Ledger applied for Round Off on a SALES voucher.', 'Round Off ledger (sales)'),
        ),
        migrations.AddField(
            model_name='tallyconfig',
            name='tds_ledger_sales',
            field=_fk('tds_sales_tally_configs', 'Ledger applied when TDS is deducted on a SALES voucher.', 'TDS ledger (sales)'),
        ),
    ]

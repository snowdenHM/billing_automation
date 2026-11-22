"""Consolidated line items are off by default on every voucher.

Client correction: "Keep Consolidated items off for all the vouchers by
default." Payment and receipt were the two that defaulted to True, and
analysis additionally flipped the flag on for any bill with two or more
lines — so the first sight of the voucher was an aggregate and the lines
the document actually carries were behind a toggle.

A default change only affects rows created from here on. Bills already
analysed keep whatever is stored on them, which is the point: nobody's
open work rearranges itself underneath them.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tally', '0059_gst_output_ledgers_and_sales_adjustments'),
    ]

    operations = [
        migrations.AlterField(
            model_name='tallypaymentanalyzedbill',
            name='consolidate',
            field=models.BooleanField(
                default=False,
                help_text=(
                    'Off by default, like every other voucher. Payment '
                    'vouchers used to open consolidated, so the operator\'s '
                    'first sight of the document was an aggregate and the '
                    'lines it actually carries were a click away. The '
                    'toggle still turns it on.'
                ),
            ),
        ),
        migrations.AlterField(
            model_name='tallyreceiptanalyzedbill',
            name='consolidate',
            field=models.BooleanField(
                default=False,
                help_text=(
                    'Off by default, like every other voucher. Receipt '
                    'vouchers used to open consolidated, so the operator\'s '
                    'first sight of the document was an aggregate and the '
                    'lines it actually carries were a click away. The '
                    'toggle still turns it on.'
                ),
            ),
        ),
    ]

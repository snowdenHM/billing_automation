# Generated manually (no local Django env available for `makemigrations`).
"""Client Correction 26: split the Payment Voucher "Vendor" slot.

Adds ``payment_mode`` — the actual Bank / Cash ledger a payment is made
through (scoped to ``TallyConfig.payment_parents`` on the frontend). The
existing ``vendor`` FK stays put but is now purely an identification
picker; it is no longer posted to the sync XML.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        # Bumped from 0037 → 0038 to sequence after the parallel
        # Additional-Adjustments migration (C16). Both agents wrote a
        # migration numbered 0038; renamed this one to 0039.
        ('tally', '0038_tallyconfig_cess_parents_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='tallypaymentanalyzedbill',
            name='payment_mode',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='payment_mode_tally_payment_analysed_bills',
                to='tally.ledger',
            ),
        ),
    ]

# Generated manually.
"""Adds a ``last_tally_ping_at`` timestamp on TallyConfig.

The Tally TCP bridge POSTs to ``/health/ping/`` every 10 minutes; each
hit stamps this column. The frontend polls the paired GET endpoint and
shows a green/red connectivity badge in the Account Info card.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tally', '0040_tallyconfig_adjustment_ledgers'),
    ]

    operations = [
        migrations.AddField(
            model_name='tallyconfig',
            name='last_tally_ping_at',
            field=models.DateTimeField(
                blank=True,
                null=True,
                verbose_name='Last Tally TCP ping',
                help_text='Timestamp of the last health ping received from the Tally TCP bridge.',
            ),
        ),
    ]

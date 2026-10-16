# Generated manually.
"""Adds ``processing_stage`` to the three Tally bill models.

Drives a real progress bar on the bills list. Until now the only signal
was the boolean ``is_processing``, so a Draft row that was mid-analysis
looked identical to one sitting idle — which is why operators kept
pressing Analyse on bills that were already being worked on.

Existing rows default to ``queued``; the value only becomes meaningful
once a bill goes through the background task again, and the serializer
reports ``done`` for anything already analysed.
"""

from django.db import migrations, models


STAGE_CHOICES = [
    ("queued", "Queued"),
    ("screening", "Checking document"),
    ("analysing", "Reading bill"),
    ("duplicate_check", "Checking duplicates"),
    ("done", "Done"),
    ("failed", "Failed"),
]


class Migration(migrations.Migration):

    dependencies = [
        ("tally", "0041_tallyconfig_last_tally_ping_at"),
    ]

    operations = [
        migrations.AddField(
            model_name=model,
            name="processing_stage",
            field=models.CharField(
                max_length=20,
                choices=STAGE_CHOICES,
                default="queued",
                blank=True,
                help_text="Which step of background analysis this bill is on.",
            ),
        )
        for model in ("tallyvendorbill", "tallyexpensebill", "tallypaymentbill")
    ]

# Generated manually.
"""Record which route read each bill, and what it cost.

The OCR-first route claims a saving. A claim about token counts has to be
measurable per bill or it stays a claim, so the route, the model and the
OCR confidence are recorded alongside everything else that happened to the
bill.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("common", "0005_billactivity")]

    operations = [
        migrations.AlterField(
            model_name="billactivity",
            name="action",
            field=models.CharField(
                max_length=32,
                choices=[
                    ("uploaded", "Uploaded"),
                    ("analysis_queued", "Analysis queued"),
                    ("analysis_completed", "Analysis completed"),
                    ("analysis_failed", "Analysis failed"),
                    ("duplicate_flagged", "Flagged as duplicate"),
                    ("extracted", "Extracted"),
                    ("verified", "Verified"),
                    ("sync_requested", "Sent to accounting software"),
                    ("sync_confirmed", "Posted"),
                    ("sync_failed", "Posting failed"),
                    ("moved", "Moved"),
                    ("trashed", "Moved to Trash"),
                    ("restored", "Restored from Trash"),
                    ("deleted", "Deleted permanently"),
                ],
            ),
        ),
    ]

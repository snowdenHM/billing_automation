import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tally", "0026_tallyconfig_round_off_parents_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="TallySetupStep",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                        unique=True,
                    ),
                ),
                (
                    "step_number",
                    models.PositiveSmallIntegerField(
                        help_text="Step number shown to the user (1, 2, 3, ...)"
                    ),
                ),
                ("title", models.CharField(max_length=200)),
                (
                    "description",
                    models.TextField(
                        help_text="Plain text or markdown — supports newlines."
                    ),
                ),
                (
                    "image",
                    models.ImageField(
                        blank=True,
                        help_text="Optional screenshot for this step.",
                        null=True,
                        upload_to="tally/setup-guide/",
                    ),
                ),
                (
                    "image_alt",
                    models.CharField(
                        blank=True,
                        help_text="Alt text for accessibility — describe the screenshot.",
                        max_length=200,
                    ),
                ),
                (
                    "order",
                    models.PositiveSmallIntegerField(
                        default=0,
                        help_text="Tiebreaker when two steps share a step_number (lower comes first).",
                    ),
                ),
                (
                    "is_active",
                    models.BooleanField(
                        default=True, help_text="Hide a step without deleting it."
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Tally Setup Step",
                "verbose_name_plural": "Tally Setup Steps",
                "ordering": ["step_number", "order"],
            },
        ),
        migrations.AddIndex(
            model_name="tallysetupstep",
            index=models.Index(
                fields=["is_active", "step_number"],
                name="tally_tally_is_acti_3a1b9c_idx",
            ),
        ),
    ]

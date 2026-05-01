import uuid

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tally", "0027_tallysetupstep"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="TallyTcpRelease",
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
                    "version",
                    models.CharField(
                        help_text="Semantic version, e.g. '1.2.3'.",
                        max_length=50,
                        unique=True,
                    ),
                ),
                (
                    "file",
                    models.FileField(
                        help_text=(
                            "Upload the .tcp file here. The latest active release "
                            "will be served to users."
                        ),
                        upload_to="tally/tcp/",
                    ),
                ),
                (
                    "notes",
                    models.TextField(
                        blank=True,
                        help_text="Optional release notes (visible only in admin).",
                    ),
                ),
                (
                    "is_active",
                    models.BooleanField(
                        default=False,
                        help_text=(
                            "Only one release should be active at a time. "
                            "Saving a release with this checked will deactivate all others."
                        ),
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "uploaded_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=models.deletion.SET_NULL,
                        related_name="tally_tcp_releases",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Tally TCP Release",
                "verbose_name_plural": "Tally TCP Releases",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="tallytcprelease",
            index=models.Index(
                fields=["is_active", "-created_at"],
                name="tally_tally_is_acti_tcp_idx",
            ),
        ),
    ]

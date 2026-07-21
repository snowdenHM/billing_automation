"""Initial migration for apps.common — creates the EmailSettings singleton."""
from django.db import migrations, models


def _seed_singleton(apps, schema_editor):
    EmailSettings = apps.get_model("common", "EmailSettings")
    EmailSettings.objects.get_or_create(
        pk=1,
        defaults={
            "is_enabled": True,
            "sendgrid_api_key": "",
            "from_email": "support@billmunshi.com",
            "from_name": "Bill Munshi",
            "reply_to": "support@billmunshi.com",
        },
    )


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="EmailSettings",
            fields=[
                ("id", models.PositiveIntegerField(default=1, editable=False, primary_key=True, serialize=False)),
                ("is_enabled", models.BooleanField(default=True, help_text="Turn off to force the console-email fallback everywhere.")),
                ("sendgrid_api_key", models.CharField(blank=True, help_text="SendGrid API key (SG.xxxx). Leave blank to use console/SMTP fallback.", max_length=256)),
                ("from_email", models.EmailField(default="support@billmunshi.com", help_text="Envelope From address. Must be a SendGrid-verified sender.", max_length=254)),
                ("from_name", models.CharField(default="Bill Munshi", help_text="Display name shown in the recipient's inbox.", max_length=100)),
                ("reply_to", models.EmailField(blank=True, default="support@billmunshi.com", help_text="Reply-To address on outbound emails.", max_length=254)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Email settings",
                "verbose_name_plural": "Email settings",
            },
        ),
        migrations.RunPython(_seed_singleton, migrations.RunPython.noop),
    ]

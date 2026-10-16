# Generated manually.
"""Make the outbound mail provider switchable from Django admin.

``EmailSettings`` only ever held a SendGrid API key, so SendGrid was the
only provider reachable without a redeploy. When that account returned
"Maximum credits exceeded" every signup lost its verification email and
there was no way to fail over.

Adds a ``provider`` choice plus SMTP credentials, so operations can point
at Brevo / Mailgun / Postmark / SES / Gmail from admin.

``provider`` defaults to ``sendgrid`` on purpose: existing installs keep
behaving exactly as before until someone deliberately switches.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("common", "0003_demorequest_email_verified_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="emailsettings",
            name="provider",
            field=models.CharField(
                max_length=20,
                default="sendgrid",
                choices=[
                    ("smtp", "SMTP (Brevo, Mailgun, SES, Gmail, …)"),
                    ("sendgrid", "SendGrid API"),
                    ("console", "Console (write to logs, send nothing)"),
                ],
                help_text=(
                    "Which service sends the mail. Existing installs default "
                    "to SendGrid so nothing changes on upgrade; switch to SMTP "
                    "to use any other provider without a redeploy."
                ),
            ),
        ),
        migrations.AddField(
            model_name="emailsettings",
            name="smtp_host",
            field=models.CharField(
                max_length=255,
                blank=True,
                default="",
                help_text=(
                    "e.g. smtp-relay.brevo.com, smtp.mailgun.org, "
                    "email-smtp.ap-south-1.amazonaws.com"
                ),
            ),
        ),
        migrations.AddField(
            model_name="emailsettings",
            name="smtp_port",
            field=models.PositiveIntegerField(
                default=587,
                help_text="587 for STARTTLS (usual), 465 for implicit SSL.",
            ),
        ),
        migrations.AddField(
            model_name="emailsettings",
            name="smtp_username",
            field=models.CharField(max_length=255, blank=True, default=""),
        ),
        migrations.AddField(
            model_name="emailsettings",
            name="smtp_password",
            field=models.CharField(max_length=512, blank=True, default=""),
        ),
        migrations.AddField(
            model_name="emailsettings",
            name="smtp_use_tls",
            field=models.BooleanField(
                default=True, help_text="STARTTLS. Use with port 587.",
            ),
        ),
        migrations.AddField(
            model_name="emailsettings",
            name="smtp_use_ssl",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Implicit SSL. Use with port 465. Do not enable together "
                    "with STARTTLS."
                ),
            ),
        ),
    ]

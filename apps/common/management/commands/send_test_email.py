"""Send one real email using the live EmailSettings configuration.

Mail problems are otherwise only discovered when a user signs up and
nothing arrives — the SendGrid "Maximum credits exceeded" outage was
found that way. This exercises the exact same path as a verification
email (same config loader, same connection resolver, same templates'
sender identity) so a provider can be proved working the moment it is
configured.

    python manage.py send_test_email you@example.com
    python manage.py send_test_email you@example.com --show-config
"""

from django.core.management.base import BaseCommand, CommandError
from django.core.mail import EmailMultiAlternatives

from apps.common.email_config import get_email_config, invalidate_email_config
from apps.common.utils import _resolve_email_connection


class Command(BaseCommand):
    help = "Send a test email through the configured provider."

    def add_arguments(self, parser):
        parser.add_argument("to", help="Recipient address.")
        parser.add_argument(
            "--show-config",
            action="store_true",
            help="Print the resolved settings (secrets masked) before sending.",
        )

    def handle(self, *args, **options):
        to = options["to"]

        # The config is cached per process; drop it so this reflects what
        # was just saved in admin rather than a stale copy.
        invalidate_email_config()
        cfg = get_email_config()

        if options["show_config"]:
            def mask(v):
                if not v:
                    return "(empty)"
                return f"{v[:4]}…{v[-2:]} ({len(v)} chars)"

            self.stdout.write("Resolved email config:")
            self.stdout.write(f"  enabled        : {cfg.is_enabled}")
            self.stdout.write(f"  provider       : {cfg.provider}")
            self.stdout.write(f"  from           : {cfg.formatted_from}")
            self.stdout.write(f"  reply-to       : {cfg.reply_to}")
            self.stdout.write(f"  use_sendgrid   : {cfg.use_sendgrid}")
            self.stdout.write(f"  use_smtp       : {cfg.use_smtp}")
            if cfg.provider == "smtp":
                self.stdout.write(f"  smtp host:port : {cfg.smtp_host}:{cfg.smtp_port}")
                self.stdout.write(f"  smtp username  : {cfg.smtp_username or '(empty)'}")
                self.stdout.write(f"  smtp password  : {mask(cfg.smtp_password)}")
                self.stdout.write(f"  tls / ssl      : {cfg.smtp_use_tls} / {cfg.smtp_use_ssl}")
            if cfg.provider == "sendgrid":
                self.stdout.write(f"  sendgrid key   : {mask(cfg.sendgrid_api_key)}")
            self.stdout.write("")

        if not cfg.is_enabled:
            self.stdout.write(
                self.style.WARNING(
                    "EmailSettings.is_enabled is off — nothing real will be sent."
                )
            )

        connection = _resolve_email_connection(cfg)
        message = EmailMultiAlternatives(
            subject="Bill Munshi — test email",
            body=(
                "This is a test message from Bill Munshi.\n\n"
                f"Provider: {cfg.provider}\n"
                f"From: {cfg.formatted_from}\n\n"
                "If you are reading this, outbound email is working."
            ),
            from_email=cfg.formatted_from,
            to=[to],
            reply_to=[cfg.reply_to] if cfg.reply_to else None,
            connection=connection,
        )

        try:
            message.send(fail_silently=False)
        except Exception as exc:
            # Surfaced in full: the provider's own message ("Maximum
            # credits exceeded", "535 authentication failed", …) is the
            # whole point of running this.
            raise CommandError(
                f"Send FAILED via {cfg.provider}:\n\n{type(exc).__name__}: {exc}"
            )

        self.stdout.write(
            self.style.SUCCESS(f"Sent to {to} via {cfg.provider}. Check the inbox.")
        )

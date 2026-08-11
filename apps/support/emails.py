"""
Email helpers for the support app.

Deliberately independent of ``apps.common.utils.send_templated_email`` —
that helper requires a matching ``.txt`` template to exist for every
``.html`` template, and this app only ships HTML templates (per spec).
Instead we render the HTML template and derive a plain-text body from it
with ``django.utils.html.strip_tags`` so the email is still multipart.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from django.template.loader import render_to_string
from django.utils.html import strip_tags

from .models import SupportTicketRecipient

logger = logging.getLogger(__name__)


def _resolve_connection():
    """Mirror ``apps.common.utils._resolve_email_connection`` — build an
    anymail SendGrid connection from the runtime DB config when enabled,
    otherwise fall back to the settings-configured EMAIL_BACKEND."""
    try:
        from apps.common.email_config import get_email_config

        cfg = get_email_config()
        if cfg.use_sendgrid:
            return get_connection(
                backend="anymail.backends.sendgrid.EmailBackend",
                api_key=cfg.sendgrid_api_key,
            ), cfg.formatted_from
        return get_connection(), cfg.formatted_from
    except Exception:  # pragma: no cover - defensive fallback
        return get_connection(), getattr(
            settings, "DEFAULT_FROM_EMAIL", "support@billmunshi.com"
        )


def _site_url() -> str:
    return getattr(settings, "SITE_URL", None) or getattr(
        settings, "FRONTEND_URL", "https://billmunshi.com"
    )


def _send_html_email(subject: str, template: str, to_email: str, context: dict):
    if not to_email:
        return
    connection, from_email = _resolve_connection()
    html_body = render_to_string(f"emails/{template}", context)
    text_body = strip_tags(html_body)

    email = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=from_email,
        to=[to_email],
        connection=connection,
    )
    email.attach_alternative(html_body, "text/html")
    try:
        email.send(fail_silently=False)
    except Exception:
        logger.exception("Failed to send support email '%s' to %s", subject, to_email)


def notify_recipients_new_ticket(ticket):
    """Email every dev-team recipient subscribed to new-ticket notices."""
    context = {
        "ticket": ticket,
        "site_url": _site_url(),
        "admin_url": f"{_site_url()}/admin/support/supportticket/{ticket.id}/change/",
    }
    subject = f"[Support] New ticket: {ticket.subject}"
    recipients = SupportTicketRecipient.objects.filter(receive_new=True)
    for recipient in recipients:
        _send_html_email(subject, "support_ticket_new.html", recipient.email, context)


def notify_reply(ticket, message, to_email: str):
    """Email the other party (submitter or dev-team) about a new reply."""
    if not to_email:
        return
    context = {
        "ticket": ticket,
        "message": message,
        "site_url": _site_url(),
        "admin_url": f"{_site_url()}/admin/support/supportticket/{ticket.id}/change/",
    }
    subject = f"[Support] New reply on: {ticket.subject}"
    _send_html_email(subject, "support_ticket_reply.html", to_email, context)


def notify_recipients_reply(ticket, message):
    """Email dev-team recipients subscribed to reply notices (used when the
    submitter is the one replying)."""
    context = {
        "ticket": ticket,
        "message": message,
        "site_url": _site_url(),
        "admin_url": f"{_site_url()}/admin/support/supportticket/{ticket.id}/change/",
    }
    subject = f"[Support] New reply on: {ticket.subject}"
    recipients = SupportTicketRecipient.objects.filter(receive_reply=True)
    for recipient in recipients:
        _send_html_email(subject, "support_ticket_reply.html", recipient.email, context)

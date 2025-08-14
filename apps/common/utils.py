from django.core.mail import send_mail
from django.conf import settings


def send_simple_email(subject: str, message: str, to_email: str, from_email: str | None = None):
    """Small helper to send a text email; uses configured EMAIL_BACKEND."""
    if not from_email:
        from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@local")
    return send_mail(subject, message, from_email, [to_email], fail_silently=True)
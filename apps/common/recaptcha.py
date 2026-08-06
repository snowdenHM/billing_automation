"""
Google reCAPTCHA v2 ("I'm not a robot") server-side verification.

``/book-demo`` and ``/auth/register`` are the only two endpoints an
anonymous caller can POST to, which makes them the cheapest targets for
scripted signup spam. Both now carry a checkbox widget, but the widget
alone proves nothing — the response token it produces is only meaningful
once Google's ``siteverify`` endpoint confirms it, and that confirmation
has to happen here, on the server, where a bot can't skip it.

Verification is opt-in by configuration: setting ``RECAPTCHA_SECRET_KEY``
turns it on, mirroring how ``SENDGRID_API_KEY`` switches the email
backend. Local development and tests therefore need no extra flag, and a
deployment can force the decision either way with ``RECAPTCHA_ENABLED``.
"""

import logging

import requests
from django.conf import settings
from rest_framework import serializers
from rest_framework.fields import empty

logger = logging.getLogger(__name__)

VERIFY_URL = "https://www.google.com/recaptcha/api/siteverify"

MISSING_TOKEN_MESSAGE = "Please complete the “I’m not a robot” check."
FAILED_MESSAGE = "Captcha verification failed. Please tick the box again."
EXPIRED_MESSAGE = "This captcha has expired. Please tick the box again."
UNAVAILABLE_MESSAGE = (
    "We couldn't verify the captcha right now. Please try again in a moment."
)

# Google's documented error codes, mapped to wording the visitor can act
# on. Codes not listed here (``invalid-input-secret``, ``bad-request``)
# describe *our* misconfiguration, so they fall through to the generic
# message rather than telling a visitor about our keys.
_ERROR_MESSAGES = {
    "missing-input-response": MISSING_TOKEN_MESSAGE,
    "invalid-input-response": FAILED_MESSAGE,
    "timeout-or-duplicate": EXPIRED_MESSAGE,
}


def get_secret_key():
    """Return the configured reCAPTCHA secret, or an empty string."""
    return (getattr(settings, "RECAPTCHA_SECRET_KEY", "") or "").strip()


def is_enabled():
    """Whether captcha checks should run for this deployment.

    ``RECAPTCHA_ENABLED`` is a tri-state: ``None`` (the default) means
    "decide from whether a secret is configured", ``True``/``False``
    force the answer.
    """
    override = getattr(settings, "RECAPTCHA_ENABLED", None)
    if override is not None:
        return bool(override)
    return bool(get_secret_key())


def get_client_ip(request):
    """Best-effort caller IP for siteverify's optional ``remoteip``.

    Nginx terminates TLS in front of this app, so the real client sits at
    the head of ``X-Forwarded-For``. The value is advisory to Google —
    a spoofed header degrades scoring, it can't bypass the check — so no
    stricter parsing is warranted.
    """
    if request is None:
        return None
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    return request.META.get("REMOTE_ADDR") or None


def verify_token(token, remote_ip=None):
    """Verify a reCAPTCHA response token.

    Returns ``(ok, message)`` — ``message`` is ``None`` when ``ok`` is
    True, and a visitor-facing string otherwise.
    """
    if not is_enabled():
        return True, None

    secret = get_secret_key()
    if not secret:
        # Forced on without a secret is a deploy mistake. Shout about it
        # in the logs, but don't lock every visitor out of signup over an
        # ops error — the form's other validation still applies.
        logger.error(
            "RECAPTCHA_ENABLED is on but RECAPTCHA_SECRET_KEY is empty; "
            "skipping captcha verification."
        )
        return True, None

    token = (token or "").strip()
    if not token:
        return False, MISSING_TOKEN_MESSAGE

    payload = {"secret": secret, "response": token}
    if remote_ip:
        payload["remoteip"] = remote_ip

    timeout = getattr(settings, "RECAPTCHA_TIMEOUT", 5.0)
    try:
        response = requests.post(VERIFY_URL, data=payload, timeout=timeout)
        response.raise_for_status()
        result = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.error("reCAPTCHA siteverify call failed: %s", exc)
        # Google being unreachable is our problem, not the visitor's —
        # but accepting everything while we can't verify is exactly the
        # hole the captcha exists to close. Fail closed, and let ops opt
        # into the opposite with RECAPTCHA_FAIL_OPEN if an outage ever
        # matters more than the spam.
        if getattr(settings, "RECAPTCHA_FAIL_OPEN", False):
            return True, None
        return False, UNAVAILABLE_MESSAGE

    if result.get("success"):
        return True, None

    codes = result.get("error-codes") or []
    logger.info("reCAPTCHA rejected a submission: %s", codes)
    for code in codes:
        if code in _ERROR_MESSAGES:
            return False, _ERROR_MESSAGES[code]
    return False, FAILED_MESSAGE


class ReCaptchaField(serializers.CharField):
    """Write-only serializer field that verifies its own value.

    Declared with ``default=""`` rather than ``required=True`` so a
    submission with no token at all still produces our own wording
    instead of DRF's generic "This field is required." — missing and
    invalid tokens then read identically to the visitor, and the
    enabled/disabled decision stays in :func:`verify_token` alone.

    The owning serializer must be given ``context={"request": request}``
    for the ``remoteip`` hint; verification still works without it.
    """

    def __init__(self, **kwargs):
        kwargs.setdefault("write_only", True)
        kwargs.setdefault("allow_blank", True)
        kwargs.setdefault("default", "")
        kwargs.setdefault(
            "help_text",
            "Google reCAPTCHA v2 response token produced by the browser widget.",
        )
        super().__init__(**kwargs)

    def run_validation(self, data=empty):
        value = super().run_validation(data)
        ok, message = verify_token(value, get_client_ip(self.context.get("request")))
        if not ok:
            raise serializers.ValidationError(message)
        return value

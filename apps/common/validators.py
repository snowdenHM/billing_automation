"""
Shared validators used across the project.
"""
import os

from django.conf import settings
from django.core.exceptions import ValidationError


# ---------------------------------------------------------------------------
# Business-email enforcement
#
# Demo requests are a sales-qualification funnel, so we only accept an
# address at a domain the person's organisation actually controls. Free
# consumer mailboxes (gmail.com, yahoo.in, …) and throwaway/disposable
# providers carry no signal about who is asking, so they're rejected.
#
# This is a denylist rather than an allowlist: we can't enumerate every
# legitimate company domain, but the set of consumer providers is small
# and stable. Extend it per-deployment with ``BLOCKED_EMAIL_DOMAINS`` in
# settings instead of editing this list.
# ---------------------------------------------------------------------------

FREE_EMAIL_DOMAINS = frozenset({
    # Global consumer providers
    "gmail.com", "googlemail.com",
    "yahoo.com", "yahoo.co.in", "yahoo.co.uk", "yahoo.in", "ymail.com", "rocketmail.com",
    "hotmail.com", "hotmail.co.uk", "outlook.com", "outlook.in", "live.com", "msn.com",
    "aol.com", "icloud.com", "me.com", "mac.com",
    "protonmail.com", "proton.me", "pm.me",
    "zoho.com", "zohomail.com",
    "gmx.com", "gmx.net", "mail.com", "inbox.com", "fastmail.com",
    "yandex.com", "yandex.ru", "tutanota.com", "hushmail.com",
    # India-specific consumer providers
    "rediffmail.com", "rediff.com", "sify.com", "indiatimes.com",
    "bsnl.in", "bsnl.co.in", "vsnl.net", "vsnl.com", "airtelmail.in",
    # Disposable / throwaway
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com",
    "temp-mail.org", "throwawaymail.com", "yopmail.com", "trashmail.com",
    "sharklasers.com", "getnada.com", "dispostable.com", "maildrop.cc",
})


def get_blocked_email_domains():
    """Return the effective set of non-business email domains.

    ``settings.BLOCKED_EMAIL_DOMAINS`` is merged on top of the built-in
    list so a deployment can block extra domains without a code change.
    """
    extra = getattr(settings, "BLOCKED_EMAIL_DOMAINS", None) or ()
    return FREE_EMAIL_DOMAINS | {str(d).strip().lower().lstrip("@") for d in extra if d}


def is_business_email(value):
    """True when ``value`` looks like a company-controlled address."""
    if not value or "@" not in str(value):
        return False
    domain = str(value).rsplit("@", 1)[1].strip().lower()
    return bool(domain) and domain not in get_blocked_email_domains()


def validate_business_email(value):
    """Reject free/personal/disposable mailboxes.

    Raises ``ValidationError`` so it works as both a model field
    validator and a DRF field-level validator.
    """
    if not is_business_email(value):
        raise ValidationError(
            "Please use your work email address. "
            "Personal email accounts (Gmail, Yahoo, Outlook, etc.) are not accepted."
        )


#: First bytes that identify each format we accept. Checked because an
#: extension is just a label the uploader chose: `.pdf` on an HTML file,
#: an SVG, a zip or a shell script passes a name check and then reaches
#: the OCR pipeline, the worker, and whatever library opens it. The
#: browser side is already covered (the serve view derives Content-Type
#: from the same extension, and production sends X-Content-Type-Options
#: nosniff) — this is about what the *parsers* are handed.
_MAGIC_BYTES = {
    ".pdf": [b"%PDF-"],
    ".png": [b"\x89PNG\r\n\x1a\n"],
    # JPEG starts FF D8 FF; the fourth byte varies by encoder (JFIF, Exif,
    # raw), so only the three-byte marker is reliable.
    ".jpg": [b"\xff\xd8\xff"],
    ".jpeg": [b"\xff\xd8\xff"],
}


def validate_file_extension(value):
    """
    Validates the file extension AND the file's actual content.
    Used by both Tally and Zoho bill models.
    """
    allowed = {".pdf", ".png", ".jpg", ".jpeg"}
    ext = os.path.splitext(getattr(value, "name", ""))[1].lower()
    if ext not in allowed:
        raise ValidationError(
            f"Unsupported file extension '{ext}'. Allowed: {', '.join(sorted(allowed))}"
        )

    signatures = _MAGIC_BYTES.get(ext)
    if not signatures:
        return

    try:
        pos = value.tell() if hasattr(value, "tell") else None
        if hasattr(value, "seek"):
            value.seek(0)
        head = value.read(8)
        if hasattr(value, "seek"):
            # Put the handle back where it was — the caller still has to
            # save this file, and a consumed stream writes zero bytes.
            value.seek(pos if pos is not None else 0)
    except Exception:
        # A storage backend that cannot be re-read here is not a reason
        # to reject an otherwise valid upload; the extension check stands.
        return

    if not head:
        raise ValidationError("The file is empty.")

    if not any(head.startswith(sig) for sig in signatures):
        raise ValidationError(
            f"This file is named '{ext}' but its contents are not a valid "
            f"{ext.lstrip('.').upper()}. Re-export it and upload again."
        )


def validate_bill_file_size(value):
    """Reject uploads larger than ``BILL_MAX_UPLOAD_BYTES`` (default 25 MB).

    Used as a model FileField validator on bill uploads. Raising here is the
    last line of defence behind ``DATA_UPLOAD_MAX_MEMORY_SIZE``.
    """
    max_bytes = getattr(settings, "BILL_MAX_UPLOAD_BYTES", 25 * 1024 * 1024)
    size = getattr(value, "size", 0) or 0
    if size > max_bytes:
        mb_limit = max_bytes // (1024 * 1024)
        raise ValidationError(f"File too large. Maximum allowed is {mb_limit} MB.")


# ---------------------------------------------------------------------------
# Per-org media path — replaces the flat ``upload_to="bills/"`` so files
# from different organisations end up under ``media/bills/<org_id>/``.
# Combined with the auth-guarded /media/bills/ serve view (#1), this gives
# multi-tenant hygiene: even if the media root is misconfigured, files
# from Org A can't collide with or be confused for files from Org B.
# ---------------------------------------------------------------------------

def _get_org_id_from_instance(instance):
    """Best-effort resolution of the owning org UUID at upload time."""
    if instance is None:
        return None
    # Direct FK column — set before ``.save()`` in the upload views.
    org_id = getattr(instance, "organization_id", None)
    if org_id:
        return org_id
    org = getattr(instance, "organization", None)
    if org is not None:
        return getattr(org, "id", None)
    return None


def bill_upload_path(instance, filename):
    """Return ``bills/<org_id>/<filename>`` for a bill FileField.

    Django calls this with (model instance, uploaded filename). We stamp
    the org id in the path so ``ls media/bills/`` returns one directory
    per tenant. Legacy files under ``bills/foo.pdf`` continue to work —
    only new uploads land in the org-scoped subdirectory.
    """
    org_id = _get_org_id_from_instance(instance)
    safe_name = filename.replace("/", "_").replace("\\", "_")
    if org_id:
        return f"bills/{org_id}/{safe_name}"
    return f"bills/{safe_name}"

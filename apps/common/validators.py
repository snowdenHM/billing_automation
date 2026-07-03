"""
Shared validators used across the project.
"""
import os

from django.conf import settings
from django.core.exceptions import ValidationError


def validate_file_extension(value):
    """
    Validates the file extension for uploads (PDF/Images only).
    Used by both Tally and Zoho bill models.
    """
    allowed = {".pdf", ".png", ".jpg", ".jpeg"}
    ext = os.path.splitext(getattr(value, "name", ""))[1].lower()
    if ext not in allowed:
        raise ValidationError(
            f"Unsupported file extension '{ext}'. Allowed: {', '.join(sorted(allowed))}"
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

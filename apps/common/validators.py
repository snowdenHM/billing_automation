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

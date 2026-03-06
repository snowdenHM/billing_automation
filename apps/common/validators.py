"""
Shared validators used across the project.
"""
import os

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

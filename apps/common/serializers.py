"""
Shared serializer components used across Tally and Zoho modules.

Centralises ``UploadedByUserSerializer``, ``FileUploadField``, and ``OrgField``
so each module can import from one place instead of redefining them.
"""

import re

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from apps.common.models import DemoRequest
from apps.common.validators import validate_business_email
from apps.organizations.models import Organization

User = get_user_model()


# ---------------------------------------------------------------------------
# Reusable fields
# ---------------------------------------------------------------------------

class FileUploadField(serializers.FileField):
    """Custom file field with validation for supported file types."""

    ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}
    MAX_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB

    def __init__(self, **kwargs):
        kwargs.setdefault("help_text", "Upload PDF, PNG, or JPG files only")
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        file = super().to_internal_value(data)

        # Validate file extension
        if hasattr(file, "name"):
            file_ext = "." + file.name.lower().rsplit(".", 1)[-1]
            if file_ext not in self.ALLOWED_EXTENSIONS:
                raise serializers.ValidationError(
                    f"Unsupported file type. Only PDF, PNG, and JPG files are allowed. Got: {file_ext}"
                )

        # Validate file size
        if hasattr(file, "size") and file.size > self.MAX_SIZE_BYTES:
            raise serializers.ValidationError(
                f"File too large. Maximum file size is 10MB. Got: {file.size / (1024 * 1024):.2f}MB"
            )

        return file


class OrgField(serializers.PrimaryKeyRelatedField):
    """PrimaryKeyRelatedField pre-wired to ``Organization`` queryset."""

    def get_queryset(self):
        return Organization.objects.all()


# ---------------------------------------------------------------------------
# Reusable model serializers
# ---------------------------------------------------------------------------

class UploadedByUserSerializer(serializers.ModelSerializer):
    """Serializer for user information in ``uploaded_by`` field."""

    class Meta:
        model = User
        fields = ["id", "username", "first_name", "last_name", "email"]
        read_only_fields = ["id", "username", "first_name", "last_name", "email"]


# ---------------------------------------------------------------------------
# Public demo booking
# ---------------------------------------------------------------------------

class DemoRequestSerializer(serializers.ModelSerializer):
    """Validates a public /book-demo submission.

    Two rules are enforced here, both server-side because the form is
    unauthenticated and anything client-only is trivially bypassed:

    1. ``email`` must be a business address (no Gmail/Yahoo/disposable).
    2. one booking per email — a repeat submission is rejected with a
       message the UI shows verbatim.
    """

    ALREADY_BOOKED_MESSAGE = (
        "Your demo is already booked with this email. "
        "Our team will reach out to you shortly."
    )

    class Meta:
        model = DemoRequest
        fields = [
            "id",
            "full_name",
            "organization",
            "accounting_software",
            "email",
            "phone",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]
        # The model's ``unique=True`` would otherwise generate DRF's
        # generic "demo request with this email already exists."
        extra_kwargs = {"email": {"validators": []}}

    def validate_full_name(self, value):
        value = value.strip()
        if len(value) < 2:
            raise serializers.ValidationError("Please enter your full name.")
        return value

    def validate_organization(self, value):
        value = value.strip()
        if len(value) < 2:
            raise serializers.ValidationError("Please enter your organization name.")
        return value

    def validate_phone(self, value):
        # Keep digits only for the length check so +91, spaces and
        # dashes in the submitted value don't fail a valid number.
        digits = re.sub(r"\D", "", value or "")
        if not 10 <= len(digits) <= 15:
            raise serializers.ValidationError("Please enter a valid phone number.")
        return value.strip()

    def validate_email(self, value):
        value = (value or "").strip().lower()

        try:
            validate_business_email(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages[0])

        if DemoRequest.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError(self.ALREADY_BOOKED_MESSAGE)

        return value

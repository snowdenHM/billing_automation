"""
Shared serializer components used across Tally and Zoho modules.

Centralises ``UploadedByUserSerializer``, ``FileUploadField``, and ``OrgField``
so each module can import from one place instead of redefining them.
"""

from django.contrib.auth import get_user_model
from rest_framework import serializers

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

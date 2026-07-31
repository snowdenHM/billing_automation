"""
Shared base serializers for Zoho bill types (vendor / expense / journal).

Each concrete bill-serializer file imports these bases and configures
``model``, ``ref_name``, and type-specific fields.  This eliminates the
identical ``get_file()``, ``get_uploaded_by_name()``, upload validation,
and ``_get_organization()`` logic that was duplicated 3×.
"""

import logging

from rest_framework import serializers

from apps.common.serializers import FileUploadField, UploadedByUserSerializer  # noqa: F401
from apps.common.views import generate_signed_bill_file_url

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Organisation lookup mixin (shared by product / consolidated / bill)
# ---------------------------------------------------------------------------

class OrgLookupMixin:
    """Resolve the current organization from instance → parent FK → context."""

    def _get_organization(self):
        if hasattr(self, "instance") and self.instance:
            if hasattr(self.instance, "organization"):
                return self.instance.organization
            if hasattr(self.instance, "zohoBill") and self.instance.zohoBill:
                return getattr(self.instance.zohoBill, "organization", None)
        ctx = getattr(self, "context", {})
        if "organization" in ctx:
            return ctx["organization"]
        request = ctx.get("request")
        if request and hasattr(request, "organization"):
            return request.organization
        return None


# ---------------------------------------------------------------------------
# Bill list serializer base
# ---------------------------------------------------------------------------

class BaseZohoBillListSerializer(serializers.ModelSerializer):
    """
    Listing serializer shared by all three Zoho bill types.

    Subclasses only need to set ``Meta.model`` and ``Meta.ref_name``.
    """

    file = serializers.SerializerMethodField()
    uploaded_by = UploadedByUserSerializer(read_only=True)
    uploaded_by_name = serializers.SerializerMethodField()
    # Snake-case aliases (#12 in the upload audit). Emit both keys so
    # legacy consumers still receive ``billmunshiName``/``fileType``
    # while new callers can move to the canonical snake_case names.
    bill_munshi_name = serializers.CharField(source="billmunshiName", read_only=True)
    file_type = serializers.CharField(source="fileType", read_only=True)

    class Meta:
        fields = [
            "id", "billmunshiName", "bill_munshi_name",
            "file", "fileType", "file_type", "analysed_data", "status",
            "process", "uploaded_by", "uploaded_by_name", "created_at", "update_at",
            "is_duplicate", "duplicate_description", "duplicate_score", "duplicate_matched_bills",
            "is_processing", "processing_error", "job_id",
            "bill_belong_your_org", "description",
        ]
        read_only_fields = [
            "id", "billmunshiName", "bill_munshi_name",
            "file", "file_type", "uploaded_by", "uploaded_by_name",
            "analysed_data", "created_at", "update_at",
            "is_duplicate", "duplicate_description", "duplicate_score", "duplicate_matched_bills",
            "is_processing", "processing_error", "job_id",
            "bill_belong_your_org", "description",
        ]

    def get_file(self, obj):
        """Return a short-lived, HMAC-signed URL for the bill file.

        ``/media/bills/…`` refuses unsigned requests — see
        ``apps.common.views.serve_bill_file``.
        """
        if not obj.file:
            return None
        return generate_signed_bill_file_url(
            obj.file, request=self.context.get("request")
        )

    def get_uploaded_by_name(self, obj):
        if obj.uploaded_by:
            if obj.uploaded_by.first_name or obj.uploaded_by.last_name:
                return f"{obj.uploaded_by.first_name} {obj.uploaded_by.last_name}".strip()
            return obj.uploaded_by.username
        return None


# ---------------------------------------------------------------------------
# Bill detail serializer base
# ---------------------------------------------------------------------------

class BaseZohoBillDetailSerializer(serializers.Serializer):
    """
    Detail serializer shared by all three Zoho bill types.

    Subclasses set ``zoho_bill_serializer_class`` and ``Meta.ref_name``.
    """

    id = serializers.UUIDField(read_only=True)
    billmunshiName = serializers.CharField(read_only=True)
    bill_munshi_name = serializers.CharField(source="billmunshiName", read_only=True)
    file = serializers.FileField(read_only=True)
    fileType = serializers.CharField(read_only=True)
    file_type = serializers.CharField(source="fileType", read_only=True)
    status = serializers.CharField(read_only=True)
    process = serializers.BooleanField(read_only=True)
    created_at = serializers.DateTimeField(read_only=True)
    update_at = serializers.DateTimeField(read_only=True)
    analysed_data = serializers.JSONField(read_only=True)
    zoho_bill = serializers.SerializerMethodField()
    next_bill = serializers.CharField(read_only=True, allow_null=True)
    previous_bill = serializers.CharField(read_only=True, allow_null=True)

    # Subclasses must set this
    zoho_bill_serializer_class = None

    def get_zoho_bill(self, obj):
        """Re-serialize the nested zoho_bill with organization context."""
        bill_rel = getattr(obj, "zoho_bill", None)
        if bill_rel is None:
            return None
        ser_cls = self.zoho_bill_serializer_class
        if ser_cls is None:
            return None
        ctx = self.context.copy()
        if hasattr(obj, "organization"):
            ctx["organization"] = obj.organization
        return ser_cls(bill_rel, context=ctx).data


# ---------------------------------------------------------------------------
# Upload serializer bases
# ---------------------------------------------------------------------------

_FILE_TYPE_CHOICES = [
    ("Single Invoice/File", "Single Invoice/File"),
    ("Multiple Invoice/File", "Multiple Invoice/File"),
]


class BaseZohoBillUploadSerializer(serializers.ModelSerializer):
    """Single-file upload — subclass with ``Meta.model`` + ``Meta.ref_name``."""

    file = FileUploadField(help_text="Single file to upload (PDF, JPG, PNG)")
    fileType = serializers.ChoiceField(
        choices=_FILE_TYPE_CHOICES,
        default="Single Invoice/File",
        help_text="Single Invoice/File or Multiple Invoice/File (PDF pages are split)",
    )

    class Meta:
        fields = ["file", "fileType"]

    def validate(self, attrs):
        f = attrs.get("file")
        if attrs.get("fileType") == "Multiple Invoice/File" and f:
            if not f.name.lower().endswith(".pdf"):
                raise serializers.ValidationError(
                    {"file": "Multiple Invoice/File type requires a PDF file for page splitting"}
                )
        return attrs

    def create(self, validated_data):
        return self.Meta.model.objects.create(**validated_data)


class BaseZohoBillMultipleUploadSerializer(serializers.Serializer):
    """Multi-file upload — subclass with ``Meta.ref_name``."""

    files = serializers.ListField(
        child=FileUploadField(),
        allow_empty=False,
        max_length=20,
        help_text="List of files to upload (PDF, JPG, PNG). Max 20.",
    )
    fileType = serializers.ChoiceField(
        choices=_FILE_TYPE_CHOICES,
        default="Single Invoice/File",
        help_text="Single Invoice/File or Multiple Invoice/File (PDF pages are split)",
    )

    def validate_files(self, value):
        if not value:
            raise serializers.ValidationError("At least one file is required")
        if len(value) > 20:
            raise serializers.ValidationError("Maximum 20 files allowed per upload")
        ft = self.initial_data.get("fileType", "Single Invoice/File")
        if ft == "Multiple Invoice/File":
            if not any(f.name.lower().endswith(".pdf") for f in value):
                raise serializers.ValidationError(
                    "Multiple Invoice/File type requires at least one PDF file"
                )
        return value

    def validate(self, attrs):
        files = attrs.get("files", [])
        ft = attrs.get("fileType")
        logger.info("Zoho bill upload validation — Files: %d, Type: %s", len(files), ft)
        return attrs

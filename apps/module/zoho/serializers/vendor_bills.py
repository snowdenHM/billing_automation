from __future__ import annotations

from rest_framework import serializers

from apps.module.zoho.models import (
    VendorBill,
    VendorZohoBill,
    VendorZohoProduct,
    ZohoVendor,
    ZohoTdsTcs,
    ZohoChartOfAccount,
    ZohoTaxes,
)
from apps.organizations.models import Organization


class FileUploadField(serializers.FileField):
    """Custom file field with validation for supported file types"""

    def __init__(self, **kwargs):
        kwargs.setdefault("help_text", "Upload PDF, PNG, or JPG files only")
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        file = super().to_internal_value(data)

        # Validate file extension
        if hasattr(file, "name"):
            allowed_extensions = [".pdf", ".png", ".jpg", ".jpeg"]
            file_ext = file.name.lower().split(".")[-1]
            if f".{file_ext}" not in allowed_extensions:
                raise serializers.ValidationError(
                    f"Unsupported file type. Only PDF, PNG, and JPG files are allowed. Got: .{file_ext}"
                )

        # Validate file size (10MB limit)
        max_size = 10 * 1024 * 1024  # 10MB
        if hasattr(file, "size") and file.size > max_size:
            raise serializers.ValidationError(
                f"File too large. Maximum file size is 10MB. Got: {file.size / (1024 * 1024):.2f}MB"
            )

        return file


class OrgField(serializers.PrimaryKeyRelatedField):
    def get_queryset(self):
        return Organization.objects.all()


# ---------- Zoho Vendor Bill Serializers ----------

class VendorZohoProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = VendorZohoProduct
        fields = [
            "id", "zohoBill", "item_name", "item_details", "chart_of_accounts",
            "taxes", "reverse_charge_tax_id", "itc_eligibility", "rate",
            "quantity", "amount", "created_at"
        ]
        read_only_fields = ["id", "zohoBill", "created_at"]


class VendorZohoBillSerializer(serializers.ModelSerializer):
    products = VendorZohoProductSerializer(many=True, read_only=True)

    class Meta:
        model = VendorZohoBill
        fields = [
            "id", "selectBill", "vendor", "bill_no", "bill_date", "total",
            "igst", "cgst", "sgst", "tds_tcs_id", "is_tax", "note",
            "created_at", "products"
        ]
        read_only_fields = ["id", "selectBill", "created_at"]


class ZohoVendorBillSerializer(serializers.ModelSerializer):
    """Serializer for Zoho Vendor Bill listing and basic operations"""

    class Meta:
        model = VendorBill
        fields = [
            "id", "billmunshiName", "file", "fileType", "status",
            "process", "created_at", "update_at"
        ]
        read_only_fields = ["id", "billmunshiName", "created_at", "update_at"]
        ref_name = "ZohoVendorBill"  # Unique component name


class ZohoVendorBillDetailSerializer(serializers.Serializer):
    """Serializer for detailed Zoho Vendor Bill view including analysis data and Zoho objects"""

    # Basic bill information
    id = serializers.UUIDField(read_only=True)
    billmunshiName = serializers.CharField(read_only=True)
    file = serializers.FileField(read_only=True)
    fileType = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)
    process = serializers.BooleanField(read_only=True)
    created_at = serializers.DateTimeField(read_only=True)
    update_at = serializers.DateTimeField(read_only=True)

    # Analysis data
    analysed_data = serializers.JSONField(read_only=True)

    # VendorZohoBill information
    zoho_bill = VendorZohoBillSerializer(read_only=True)

    class Meta:
        ref_name = "ZohoVendorBillDetail"


class VendorBillUploadSerializer(serializers.ModelSerializer):
    """
    Enhanced multipart upload for vendor bills. Supports both single and multiple files.
    Accepts PDF, PNG, and JPG files with proper validation.

    fileType controls split/merge behavior:
      - "Single Invoice/File": all PDF pages analysed as a single invoice
      - "Multiple Invoice/File": each PDF page becomes its own bill+analysis
    """
    organization = OrgField()
    file = FileUploadField(
        required=False,
        help_text="Upload a single PDF, PNG, or JPG file"
    )
    files = serializers.ListField(
        child=FileUploadField(),
        required=False,
        help_text="Upload multiple PDF, PNG, or JPG files"
    )
    fileType = serializers.ChoiceField(
        choices=[
            ("Single Invoice/File", "Single Invoice/File"),
            ("Multiple Invoice/File", "Multiple Invoice/File")
        ],
        default="Single Invoice/File",
        help_text="How to process the uploaded files"
    )

    class Meta:
        model = VendorBill
        fields = ["organization", "file", "files", "fileType"]

    def validate(self, attrs):
        has_file = attrs.get("file") is not None
        has_files = attrs.get("files") is not None and len(attrs.get("files", [])) > 0

        if not has_file and not has_files:
            raise serializers.ValidationError({
                "file": "Either 'file' or 'files' field is required."
            })

        if has_file and has_files:
            raise serializers.ValidationError({
                "file": "Please provide either 'file' or 'files', not both."
            })

        # Validate total number of files
        total_files = 0
        if has_file:
            total_files = 1
        elif has_files:
            total_files = len(attrs.get("files", []))

        if total_files > 20:  # Reasonable limit
            raise serializers.ValidationError({
                "files": f"Too many files. Maximum 20 files allowed. Got: {total_files}"
            })

        return attrs


class ZohoVendorBillUploadSerializer(serializers.ModelSerializer):
    """Serializer for uploading Zoho Vendor Bill files"""

    file = FileUploadField()

    class Meta:
        model = VendorBill
        fields = ["file", "fileType"]
        ref_name = "ZohoVendorBillUploadRequest"

    def create(self, validated_data):
        return VendorBill.objects.create(**validated_data)


class VerifyProductItemSerializer(serializers.Serializer):
    """
    Edits to each product during verification.
    """
    id = serializers.UUIDField()
    chart_of_accounts = serializers.PrimaryKeyRelatedField(
        queryset=ZohoChartOfAccount.objects.all(), required=False, allow_null=True
    )
    taxes = serializers.PrimaryKeyRelatedField(
        queryset=ZohoTaxes.objects.all(), required=False, allow_null=True
    )
    reverse_charge_tax_id = serializers.BooleanField(required=False)
    itc_eligibility = serializers.ChoiceField(
        choices=("eligible", "ineligible_section17", "ineligible_others"), required=False
    )


class ZohoVerifyProductItemSerializer(serializers.Serializer):
    """Edits to each product during verification."""

    id = serializers.UUIDField()
    chart_of_accounts = serializers.PrimaryKeyRelatedField(
        queryset=ZohoChartOfAccount.objects.all(), required=False, allow_null=True
    )
    taxes = serializers.PrimaryKeyRelatedField(
        queryset=ZohoTaxes.objects.all(), required=False, allow_null=True
    )
    reverse_charge_tax_id = serializers.BooleanField(required=False)
    itc_eligibility = serializers.ChoiceField(
        choices=("eligible", "ineligible_section17", "ineligible_others"), required=False
    )


class ZohoVendorBillVerifySerializer(serializers.Serializer):
    """Verification payload for the analysed bill header + products."""

    vendor = serializers.PrimaryKeyRelatedField(
        queryset=ZohoVendor.objects.all(), required=False, allow_null=True
    )
    note = serializers.CharField(required=False, allow_blank=True)
    bill_no = serializers.CharField(required=False, allow_blank=True)
    bill_date = serializers.DateField(required=False, allow_null=True)
    cgst = serializers.CharField(required=False, allow_blank=True)
    sgst = serializers.CharField(required=False, allow_blank=True)
    igst = serializers.CharField(required=False, allow_blank=True)
    tax_type = serializers.ChoiceField(choices=("TDS", "TCS", "No"), required=False, default="No")
    tds_tcs_id = serializers.PrimaryKeyRelatedField(
        queryset=ZohoTdsTcs.objects.all(), required=False, allow_null=True
    )
    products = ZohoVerifyProductItemSerializer(many=True, required=False)

    def validate(self, attrs):
        tax_type = attrs.get("tax_type")
        tds_tcs = attrs.get("tds_tcs_id")
        if tax_type in ("TDS", "TCS") and not tds_tcs:
            raise serializers.ValidationError({"tds_tcs_id": "Required when tax_type is TDS or TCS."})
        return attrs


# Response serializers
class ZohoSyncResultSerializer(serializers.Serializer):
    """Response serializer for sync operations"""
    detail = serializers.CharField()
    synced_count = serializers.IntegerField()

    class Meta:
        ref_name = "ZohoSyncResult"


class ZohoAnalysisResultSerializer(serializers.Serializer):
    """Response serializer for analysis operations"""
    detail = serializers.CharField()
    analyzed_data = serializers.JSONField()

    class Meta:
        ref_name = "ZohoAnalysisResult"


class ZohoOperationResultSerializer(serializers.Serializer):
    """Response serializer for general operations"""
    detail = serializers.CharField()

    class Meta:
        ref_name = "ZohoOperationResult"

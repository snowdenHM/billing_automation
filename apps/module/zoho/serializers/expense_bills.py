from __future__ import annotations

from django.contrib.auth.models import User
from rest_framework import serializers

from apps.module.zoho.models import (
    ExpenseBill,
    ExpenseZohoBill,
    ExpenseZohoProduct,
    ZohoVendor,
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


class UploadedByUserSerializer(serializers.ModelSerializer):
    """Serializer for user information in uploaded_by field"""

    class Meta:
        model = User
        fields = ['id', 'username', 'first_name', 'last_name', 'email']
        read_only_fields = ['id', 'username', 'first_name', 'last_name', 'email']


# ---------- Zoho Expense Bill Serializers ----------

class ExpenseZohoProductSerializer(serializers.ModelSerializer):
    """Serializer for Expense product line items with correct field mapping to model"""

    class Meta:
        model = ExpenseZohoProduct
        fields = [
            "id", "zohoBill", "item_details", "amount", "chart_of_accounts", "taxes", "created_at"
        ]
        read_only_fields = ["id", "zohoBill", "created_at"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Scope related fields to organization if context is provided through parent
        request = self.context.get('request') if hasattr(self, 'context') else None
        if request and hasattr(request, 'organization'):
            organization = request.organization
            from ..models import ZohoChartOfAccount, ZohoTaxes
            self.fields['chart_of_accounts'].queryset = ZohoChartOfAccount.objects.filter(organization=organization)
            self.fields['taxes'].queryset = ZohoTaxes.objects.filter(organization=organization)


class ExpenseZohoBillSerializer(serializers.ModelSerializer):
    """Serializer for Expense Zoho bill with corrected product relationship"""

    products = ExpenseZohoProductSerializer(many=True, read_only=True)

    class Meta:
        model = ExpenseZohoBill
        fields = [
            "id", "selectBill", "vendor", "bill_no", "bill_date", "due_date", "total", "chart_of_accounts",
            "igst", "cgst", "sgst", "note", "created_at", "products"
        ]
        read_only_fields = ["id", "selectBill", "created_at"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Scope vendor and chart_of_accounts querysets to organization if context is provided
        if 'context' in kwargs and 'organization' in kwargs['context']:
            organization = kwargs['context']['organization']
            from ..models import ZohoVendor, ZohoChartOfAccount
            self.fields['vendor'].queryset = ZohoVendor.objects.filter(organization=organization)
            self.fields['chart_of_accounts'].queryset = ZohoChartOfAccount.objects.filter(organization=organization)


class ZohoExpenseBillSerializer(serializers.ModelSerializer):
    """Serializer for Zoho Expense Bill listing and basic operations"""

    file = serializers.SerializerMethodField()
    uploaded_by = UploadedByUserSerializer(read_only=True)
    uploaded_by_name = serializers.SerializerMethodField()

    class Meta:
        model = ExpenseBill
        fields = [
            "id", "billmunshiName", "file", "fileType", "status",
            "process", "uploaded_by", "uploaded_by_name", "created_at", "update_at"
        ]
        read_only_fields = ["id", "billmunshiName", "file", "uploaded_by", "uploaded_by_name", "created_at",
                            "update_at"]
        ref_name = "ZohoExpenseBill"  # Unique component name

    def get_file(self, obj):
        """Return complete file URL"""
        if obj.file:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.file.url)
            else:
                # Fallback if no request context
                return obj.file.url
        return None

    def get_uploaded_by_name(self, obj):
        """Return formatted name of the user who uploaded the bill"""
        if obj.uploaded_by:
            if obj.uploaded_by.first_name or obj.uploaded_by.last_name:
                return f"{obj.uploaded_by.first_name} {obj.uploaded_by.last_name}".strip()
            return obj.uploaded_by.username
        return None


class ZohoExpenseBillDetailSerializer(serializers.Serializer):
    """Serializer for detailed Zoho Expense Bill view including analysis data and Zoho objects"""

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

    # ExpenseZohoBill information
    zoho_bill = ExpenseZohoBillSerializer(read_only=True)
    next_bill = serializers.CharField(read_only=True, allow_null=True)

    class Meta:
        ref_name = "ZohoExpenseBillDetail"


class ZohoExpenseBillUploadSerializer(serializers.ModelSerializer):
    """Serializer for uploading single Zoho Expense Bill file"""

    file = FileUploadField(help_text="Single file to upload (PDF, JPG, PNG)")
    fileType = serializers.ChoiceField(
        choices=[
            ("Single Invoice/File", "Single Invoice/File"),
            ("Multiple Invoice/File", "Multiple Invoice/File"),
        ],
        default="Single Invoice/File",
        help_text="Type of file upload: Single Invoice/File (file is a separate bill) or Multiple Invoice/File (PDF pages are split into separate bills)"
    )

    class Meta:
        model = ExpenseBill
        fields = ["file", "fileType"]
        ref_name = "ZohoExpenseBillUploadRequest"

    def validate(self, attrs):
        """Validate the upload data"""
        file = attrs.get('file')
        file_type = attrs.get('fileType')

        # For Multiple Invoice/File type with single file, ensure it's a PDF
        if file_type == 'Multiple Invoice/File' and file:
            if not file.name.lower().endswith('.pdf'):
                raise serializers.ValidationError({
                    'file': 'Multiple Invoice/File type requires a PDF file for page splitting'
                })

        return attrs

    def create(self, validated_data):
        return ExpenseBill.objects.create(**validated_data)


class ZohoExpenseBillMultipleUploadSerializer(serializers.Serializer):
    """Serializer for uploading multiple Zoho Expense Bill files"""

    files = serializers.ListField(
        child=FileUploadField(),
        allow_empty=False,
        max_length=20,  # Limit to 20 files max
        help_text="List of files to upload (PDF, JPG, PNG). Can accept single file or multiple files."
    )
    fileType = serializers.ChoiceField(
        choices=[
            ("Single Invoice/File", "Single Invoice/File"),
            ("Multiple Invoice/File", "Multiple Invoice/File"),
        ],
        default="Single Invoice/File",
        help_text="Type of file upload: Single Invoice/File (each file is a separate bill) or Multiple Invoice/File (PDF pages are split into separate bills)"
    )

    class Meta:
        ref_name = "ZohoExpenseBillMultipleUploadRequest"

    def validate_files(self, value):
        """Validate uploaded files"""
        if not value:
            raise serializers.ValidationError("At least one file is required")

        # Limit total number of files to prevent abuse
        if len(value) > 20:
            raise serializers.ValidationError("Maximum 20 files allowed per upload")

        # Additional validation for Multiple Invoice/File type
        file_type = self.initial_data.get('fileType', 'Single Invoice/File')
        if file_type == 'Multiple Invoice/File':
            # For Multiple Invoice/File type, check if any PDFs are included
            pdf_files = [f for f in value if f.name.lower().endswith('.pdf')]
            if not pdf_files:
                raise serializers.ValidationError(
                    "Multiple Invoice/File type requires at least one PDF file for page splitting")

        return value

    def validate(self, attrs):
        """Cross-field validation"""
        files = attrs.get('files', [])
        file_type = attrs.get('fileType')

        # Log the upload attempt
        import logging
        logger = logging.getLogger(__name__)
        logger.info(f"Zoho Expense bill upload validation - Files: {len(files)}, Type: {file_type}")

        return attrs


class ZohoExpenseVerifyProductItemSerializer(serializers.Serializer):
    """Edits to each product during Expense verification - using correct field names"""

    id = serializers.UUIDField()
    chart_of_accounts = serializers.PrimaryKeyRelatedField(
        queryset=ZohoChartOfAccount.objects.all(), required=False, allow_null=True
    )
    taxes = serializers.PrimaryKeyRelatedField(
        queryset=ZohoTaxes.objects.all(), required=False, allow_null=True
    )
    item_details = serializers.CharField(required=False, allow_blank=True)
    amount = serializers.CharField(required=False, allow_blank=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Scope querysets to organization if context is provided
        organization = self.context.get('organization') if hasattr(self, 'context') else None
        if organization:
            self.fields['chart_of_accounts'].queryset = ZohoChartOfAccount.objects.filter(organization=organization)
            self.fields['taxes'].queryset = ZohoTaxes.objects.filter(organization=organization)


class ZohoExpenseBillVerifySerializer(serializers.Serializer):
    """Verification payload for the analysed Expense bill header + products"""

    vendor = serializers.PrimaryKeyRelatedField(
        queryset=ZohoVendor.objects.all(), required=False, allow_null=True
    )
    note = serializers.CharField(required=False, allow_blank=True)
    bill_no = serializers.CharField(required=False, allow_blank=True)
    bill_date = serializers.DateField(required=False, allow_null=True)
    due_date = serializers.DateField(required=False, allow_null=True)
    cgst = serializers.CharField(required=False, allow_blank=True)
    sgst = serializers.CharField(required=False, allow_blank=True)
    igst = serializers.CharField(required=False, allow_blank=True)
    total = serializers.CharField(required=False, allow_blank=True)
    products = ZohoExpenseVerifyProductItemSerializer(many=True, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Scope vendor queryset to organization if context is provided
        organization = self.context.get('organization') if hasattr(self, 'context') else None
        if organization:
            self.fields['vendor'].queryset = ZohoVendor.objects.filter(organization=organization)

    class Meta:
        ref_name = "ZohoExpenseBillVerify"

from __future__ import annotations

from rest_framework import serializers
from django.contrib.auth.models import User

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


class UploadedByUserSerializer(serializers.ModelSerializer):
    """Serializer for user information in uploaded_by field"""
    class Meta:
        model = User
        fields = ['id', 'username', 'first_name', 'last_name', 'email']
        read_only_fields = ['id', 'username', 'first_name', 'last_name', 'email']


# ---------- Zoho Vendor Bill Serializers ----------

class VendorZohoProductSerializer(serializers.ModelSerializer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Scope foreign key fields to current organization
        organization = self._get_organization()
            
        if organization:
            self.fields['chart_of_accounts'].queryset = ZohoChartOfAccount.objects.filter(
                organization=organization
            )
            self.fields['taxes'].queryset = ZohoTaxes.objects.filter(
                organization=organization
            )
    
    def _get_organization(self):
        """Get organization from various sources"""
        # Try to get organization from instance
        if self.instance and hasattr(self.instance, 'organization'):
            return self.instance.organization
        # Try to get organization from zohoBill
        elif self.instance and hasattr(self.instance, 'zohoBill') and hasattr(self.instance.zohoBill, 'organization'):
            return self.instance.zohoBill.organization
        # Try to get organization from context
        elif hasattr(self, 'context') and 'organization' in self.context:
            return self.context['organization']
        return None

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
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # Get organization for field scoping
        organization = self._get_organization()
        
        # Scope foreign key fields to current organization
        if organization:
            self.fields['vendor'].queryset = ZohoVendor.objects.filter(
                organization=organization
            )
            self.fields['tds_tcs_id'].queryset = ZohoTdsTcs.objects.filter(
                organization=organization
            )
            self.fields['discount_account'].queryset = ZohoChartOfAccount.objects.filter(
                organization=organization
            )
    
    def _get_organization(self):
        """Get organization from instance or context"""
        if self.instance and hasattr(self.instance, 'organization'):
            return self.instance.organization
        elif hasattr(self, 'context') and 'organization' in self.context:
            return self.context['organization']
        return None
    
    def to_representation(self, instance):
        """Override to pass organization context to nested products serializer"""
        data = super().to_representation(instance)
        
        # Get organization for products serialization
        organization = self._get_organization()
        if organization and instance:
            # Re-serialize products with organization context
            products_context = self.context.copy() if self.context else {}
            products_context['organization'] = organization
            
            products_serializer = VendorZohoProductSerializer(
                instance.products.all(), 
                many=True, 
                context=products_context
            )
            data['products'] = products_serializer.data
        
        return data

    class Meta:
        model = VendorZohoBill
        fields = [
            "id", "selectBill", "vendor", "bill_no", "bill_date", "due_date", "total",
            "discount_type", "discount_amount", "discount_account", "adjustment_amount", "adjustment_description",
            "igst", "cgst", "sgst", "tds_tcs_id", "is_tax", "note",
            "created_at", "products"
        ]
        read_only_fields = ["id", "selectBill", "created_at"]


class ZohoVendorBillSerializer(serializers.ModelSerializer):
    """Serializer for Zoho Vendor Bill listing and basic operations"""

    file = serializers.SerializerMethodField()
    uploaded_by = UploadedByUserSerializer(read_only=True)
    uploaded_by_name = serializers.SerializerMethodField()

    class Meta:
        model = VendorBill
        fields = [
            "id", "billmunshiName", "file", "fileType", "status",
            "process", "uploaded_by", "uploaded_by_name", "created_at", "update_at"
        ]
        read_only_fields = ["id", "billmunshiName", "file", "uploaded_by", "uploaded_by_name", "created_at", "update_at"]
        ref_name = "ZohoVendorBill"  # Unique component name

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
    next_bill = serializers.CharField(read_only=True, allow_null=True)

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
    """Serializer for uploading single Zoho Vendor Bill file"""

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
        model = VendorBill
        fields = ["file", "fileType"]
        ref_name = "ZohoVendorBillUploadRequest"

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
        return VendorBill.objects.create(**validated_data)


class ZohoVendorBillMultipleUploadSerializer(serializers.Serializer):
    """Serializer for uploading multiple Zoho Vendor Bill files"""

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
        ref_name = "ZohoVendorBillMultipleUploadRequest"

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
                raise serializers.ValidationError("Multiple Invoice/File type requires at least one PDF file for page splitting")

        return value

    def validate(self, attrs):
        """Cross-field validation"""
        files = attrs.get('files', [])
        file_type = attrs.get('fileType')
        
        # Log the upload attempt
        import logging
        logger = logging.getLogger(__name__)
        logger.info(f"Zoho vendor bill upload validation - Files: {len(files)}, Type: {file_type}")
        
        return attrs


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
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Scope foreign key fields to current organization
        if hasattr(self, 'context') and 'organization' in self.context:
            organization = self.context['organization']
            self.fields['chart_of_accounts'].queryset = ZohoChartOfAccount.objects.filter(
                organization=organization
            )
            self.fields['taxes'].queryset = ZohoTaxes.objects.filter(
                organization=organization
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
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Scope foreign key fields to current organization
        if hasattr(self, 'context') and 'organization' in self.context:
            organization = self.context['organization']
            self.fields['chart_of_accounts'].queryset = ZohoChartOfAccount.objects.filter(
                organization=organization
            )
            self.fields['taxes'].queryset = ZohoTaxes.objects.filter(
                organization=organization
            )


class ZohoVendorBillVerifySerializer(serializers.Serializer):
    """Verification payload for the analysed bill header + products."""

    vendor = serializers.PrimaryKeyRelatedField(
        queryset=ZohoVendor.objects.all(), required=False, allow_null=True
    )
    note = serializers.CharField(required=False, allow_blank=True)
    bill_no = serializers.CharField(required=False, allow_blank=True)
    bill_date = serializers.DateField(required=False, allow_null=True)
    due_date = serializers.DateField(required=False, allow_null=True)
    discount_type = serializers.ChoiceField(
        choices=[("INR", "INR"), ("Percentage", "Percentage")],
        required=False,
        allow_null=True
    )
    discount_amount = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        required=False,
        allow_null=True
    )
    discount_account = serializers.PrimaryKeyRelatedField(
        queryset=ZohoChartOfAccount.objects.all(), required=False, allow_null=True
    )
    adjustment_amount = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        required=False,
        allow_null=True
    )
    adjustment_description = serializers.CharField(required=False, allow_blank=True)
    cgst = serializers.CharField(required=False, allow_blank=True)
    sgst = serializers.CharField(required=False, allow_blank=True)
    igst = serializers.CharField(required=False, allow_blank=True)
    tax_type = serializers.ChoiceField(choices=("TDS", "TCS", "No"), required=False, default="No")
    tds_tcs_id = serializers.PrimaryKeyRelatedField(
        queryset=ZohoTdsTcs.objects.all(), required=False, allow_null=True
    )
    products = ZohoVerifyProductItemSerializer(many=True, required=False)
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Scope foreign key fields to current organization
        if hasattr(self, 'context') and 'organization' in self.context:
            organization = self.context['organization']
            self.fields['vendor'].queryset = ZohoVendor.objects.filter(
                organization=organization
            )
            self.fields['tds_tcs_id'].queryset = ZohoTdsTcs.objects.filter(
                organization=organization
            )
            self.fields['discount_account'].queryset = ZohoChartOfAccount.objects.filter(
                organization=organization
            )

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

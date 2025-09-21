from __future__ import annotations
from rest_framework import serializers

from apps.organizations.models import Organization
from apps.module.zoho.models import (
    ExpenseBill,
    ExpenseZohoBill,
    ExpenseZohoProduct,
    ZohoVendor,
    ZohoChartOfAccount,
)


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
                f"File too large. Maximum file size is 10MB. Got: {file.size / (1024*1024):.2f}MB"
            )

        return file


class OrgField(serializers.PrimaryKeyRelatedField):
    def get_queryset(self):
        return Organization.objects.all()


# ---------- Zoho Expense Bill Serializers ----------

class ExpenseZohoProductSerializer(serializers.ModelSerializer):
    """Serializer for expense product line items with correct field mapping to model"""

    class Meta:
        model = ExpenseZohoProduct
        fields = [
            "id", "zohoBill", "item_details", "chart_of_accounts",
            "vendor", "amount", "debit_or_credit", "created_at"
        ]
        read_only_fields = ["id", "zohoBill", "created_at"]


class ExpenseZohoBillSerializer(serializers.ModelSerializer):
    """Serializer for expense Zoho bill with corrected product relationship"""

    products = ExpenseZohoProductSerializer(many=True, read_only=True)

    class Meta:
        model = ExpenseZohoBill
        fields = [
            "id", "selectBill", "vendor", "bill_no", "bill_date", "total",
            "igst", "cgst", "sgst", "note", "created_at", "products"
        ]
        read_only_fields = ["id", "selectBill", "created_at"]


class ZohoExpenseBillSerializer(serializers.ModelSerializer):
    """Serializer for Zoho Expense Bill listing and basic operations"""

    class Meta:
        model = ExpenseBill
        fields = [
            "id", "billmunshiName", "file", "fileType", "status",
            "process", "created_at", "update_at"
        ]
        read_only_fields = ["id", "billmunshiName", "created_at", "update_at"]
        ref_name = "ZohoExpenseBill"  # Unique component name


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

    class Meta:
        ref_name = "ZohoExpenseBillDetail"


class ZohoExpenseBillUploadSerializer(serializers.ModelSerializer):
    """Serializer for uploading Zoho Expense Bill files"""

    file = FileUploadField()

    class Meta:
        model = ExpenseBill
        fields = ["file", "fileType"]
        ref_name = "ZohoExpenseBillUploadRequest"

    def create(self, validated_data):
        return ExpenseBill.objects.create(**validated_data)


class ZohoExpenseVerifyProductItemSerializer(serializers.Serializer):
    """Edits to each product during expense verification - using correct field names"""

    id = serializers.UUIDField()
    chart_of_accounts = serializers.PrimaryKeyRelatedField(
        queryset=ZohoChartOfAccount.objects.all(), required=False, allow_null=True
    )
    vendor = serializers.PrimaryKeyRelatedField(
        queryset=ZohoVendor.objects.all(), required=False, allow_null=True
    )
    item_details = serializers.CharField(required=False, allow_blank=True)
    amount = serializers.CharField(required=False, allow_blank=True)
    debit_or_credit = serializers.ChoiceField(
        choices=[("credit", "Credit"), ("debit", "Debit")],
        required=False,
        default="credit"
    )


class ZohoExpenseBillVerifySerializer(serializers.Serializer):
    """Verification payload for the analysed expense bill header + products"""

    vendor = serializers.PrimaryKeyRelatedField(
        queryset=ZohoVendor.objects.all(), required=False, allow_null=True
    )
    note = serializers.CharField(required=False, allow_blank=True)
    bill_no = serializers.CharField(required=False, allow_blank=True)
    bill_date = serializers.DateField(required=False, allow_null=True)
    cgst = serializers.CharField(required=False, allow_blank=True)
    sgst = serializers.CharField(required=False, allow_blank=True)
    igst = serializers.CharField(required=False, allow_blank=True)
    total = serializers.CharField(required=False, allow_blank=True)
    products = ZohoExpenseVerifyProductItemSerializer(many=True, required=False)

    class Meta:
        ref_name = "ZohoExpenseBillVerify"

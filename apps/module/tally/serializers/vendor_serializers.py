from rest_framework import serializers
from drf_spectacular.utils import extend_schema_field
from typing import List, Dict, Any
from decimal import Decimal, InvalidOperation
from ..models import TallyVendorBill, TallyVendorAnalyzedBill, TallyVendorAnalyzedProduct, Ledger


class SafeDecimalField(serializers.DecimalField):
    """Custom decimal field that handles invalid decimal values gracefully"""

    def to_representation(self, value):
        if value is None:
            return None

        try:
            # Convert to Decimal if it's not already
            if not isinstance(value, Decimal):
                value = Decimal(str(value))

            # Check for invalid decimal values
            if value.is_nan() or value.is_infinite():
                return "0.00"

            return super().to_representation(value)
        except (InvalidOperation, ValueError, TypeError):
            # Return 0.00 for any invalid decimal values
            return "0.00"


class TallyVendorBillSerializer(serializers.ModelSerializer):
    class Meta:
        model = TallyVendorBill
        fields = [
            'id', 'bill_munshi_name', 'file', 'file_type', 'analysed_data',
            'status', 'process', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'bill_munshi_name', 'created_at', 'updated_at']

    def validate_file(self, value):
        """Validate file extension and size"""
        if value:
            # Check file extension
            allowed_extensions = ['.pdf', '.jpg', '.jpeg', '.png']
            file_extension = value.name.lower().split('.')[-1]
            if f'.{file_extension}' not in allowed_extensions:
                raise serializers.ValidationError(
                    f"Unsupported file type. Allowed types: {', '.join(allowed_extensions)}"
                )

            # Check file size (10MB limit)
            if value.size > 10 * 1024 * 1024:
                raise serializers.ValidationError("File size cannot exceed 10MB")

        return value


class TallyVendorAnalyzedProductSerializer(serializers.ModelSerializer):
    taxes_name = serializers.CharField(source='taxes.name', read_only=True)

    # Use SafeDecimalField for all decimal fields that might have invalid values
    price = SafeDecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)
    amount = SafeDecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)
    igst = SafeDecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)
    cgst = SafeDecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)
    sgst = SafeDecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)

    class Meta:
        model = TallyVendorAnalyzedProduct
        fields = [
            'id', 'item_name', 'item_details', 'taxes', 'taxes_name',
            'price', 'quantity', 'amount', 'product_gst',
            'igst', 'cgst', 'sgst', 'created_at'
        ]
        read_only_fields = ['id', 'created_at', 'taxes_name']


class TallyVendorAnalyzedBillSerializer(serializers.ModelSerializer):
    products = TallyVendorAnalyzedProductSerializer(many=True, read_only=True)
    vendor_name = serializers.CharField(source='vendor.name', read_only=True)
    selected_bill_name = serializers.CharField(source='selected_bill.bill_munshi_name', read_only=True)

    # Use SafeDecimalField for all decimal fields that might have invalid values
    total = SafeDecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)
    igst = SafeDecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)
    cgst = SafeDecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)
    sgst = SafeDecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)

    class Meta:
        model = TallyVendorAnalyzedBill
        fields = [
            'id', 'selected_bill', 'selected_bill_name', 'vendor', 'vendor_name',
            'bill_no', 'bill_date', 'total', 'igst', 'igst_taxes',
            'cgst', 'cgst_taxes', 'sgst', 'sgst_taxes', 'gst_type',
            'note', 'products', 'created_at'
        ]
        read_only_fields = ['id', 'created_at', 'vendor_name', 'selected_bill_name', 'products']


class VendorBillUploadSerializer(serializers.Serializer):
    """Serializer for multiple file upload"""
    files = serializers.ListField(
        child=serializers.FileField(allow_empty_file=False),
        help_text="List of files to upload (PDF, JPG, PNG)"
    )
    file_type = serializers.ChoiceField(
        choices=TallyVendorBill.BillType.choices,
        default=TallyVendorBill.BillType.SINGLE
    )

    def validate_files(self, value):
        """Validate uploaded files"""
        if not value:
            raise serializers.ValidationError("At least one file is required")

        for file in value:
            # Check file extension
            allowed_extensions = ['.pdf', '.jpg', '.jpeg', '.png']
            file_extension = file.name.lower().split('.')[-1]
            if f'.{file_extension}' not in allowed_extensions:
                raise serializers.ValidationError(
                    f"Unsupported file type: {file.name}. Allowed: {', '.join(allowed_extensions)}"
                )

            # Check file size (10MB per file)
            if file.size > 10 * 1024 * 1024:
                raise serializers.ValidationError(f"File {file.name} exceeds 10MB limit")

        return value


class BillAnalysisRequestSerializer(serializers.Serializer):
    """Serializer for bill analysis request"""
    bill_id = serializers.UUIDField(help_text="UUID of the bill to analyze")


class BillVerificationSerializer(serializers.Serializer):
    """Serializer for bill verification data"""
    vendor_id = serializers.UUIDField(required=False, allow_null=True)
    bill_no = serializers.CharField(max_length=50, required=False)
    bill_date = serializers.DateField(required=False, allow_null=True)
    note = serializers.CharField(max_length=500, required=False)
    igst = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, default=0)
    cgst = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, default=0)
    sgst = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, default=0)
    igst_taxes_id = serializers.UUIDField(required=False, allow_null=True)
    cgst_taxes_id = serializers.UUIDField(required=False, allow_null=True)
    sgst_taxes_id = serializers.UUIDField(required=False, allow_null=True)

    products = serializers.ListField(
        child=serializers.DictField(),
        required=False,
        help_text="List of product updates"
    )

    def validate_products(self, value):
        """Validate products data"""
        for product in value:
            if 'id' not in product:
                raise serializers.ValidationError("Product ID is required for each product")
        return value


class BillSyncRequestSerializer(serializers.Serializer):
    """Serializer for bill sync request"""
    bill_id = serializers.UUIDField(help_text="UUID of the bill to sync")


class BillSyncResponseSerializer(serializers.Serializer):
    """Serializer for bill sync response data"""
    id = serializers.UUIDField()
    bill_no = serializers.CharField()
    bill_date = serializers.CharField(allow_null=True)
    total = serializers.FloatField()
    igst = serializers.FloatField()
    cgst = serializers.FloatField()
    sgst = serializers.FloatField()
    vendor = serializers.DictField()
    customer_id = serializers.UUIDField(allow_null=True)
    transactions = serializers.ListField(child=serializers.DictField())

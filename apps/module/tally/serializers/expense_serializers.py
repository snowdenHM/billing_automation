from rest_framework import serializers
from drf_spectacular.utils import extend_schema_field
from typing import List, Dict, Any
from ..models import TallyExpenseBill, TallyExpenseAnalyzedBill, TallyExpenseAnalyzedProduct, Ledger


class TallyExpenseBillSerializer(serializers.ModelSerializer):
    file = serializers.SerializerMethodField()

    class Meta:
        model = TallyExpenseBill
        fields = [
            'id', 'bill_munshi_name', 'file', 'file_type', 'analysed_data',
            'status', 'process', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'bill_munshi_name', 'file', 'created_at', 'updated_at']

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


class TallyExpenseAnalyzedProductSerializer(serializers.ModelSerializer):
    chart_of_accounts_name = serializers.CharField(source='chart_of_accounts.name', read_only=True)

    class Meta:
        model = TallyExpenseAnalyzedProduct
        fields = [
            'id', 'item_details', 'chart_of_accounts', 'chart_of_accounts_name',
            'amount', 'debit_or_credit', 'created_at'
        ]
        read_only_fields = ['id', 'created_at', 'chart_of_accounts_name']


class TallyExpenseAnalyzedBillSerializer(serializers.ModelSerializer):
    products = TallyExpenseAnalyzedProductSerializer(many=True, read_only=True)
    vendor_name = serializers.CharField(source='vendor.name', read_only=True)
    selected_bill_name = serializers.CharField(source='selected_bill.bill_munshi_name', read_only=True)

    class Meta:
        model = TallyExpenseAnalyzedBill
        fields = [
            'id', 'selected_bill', 'selected_bill_name', 'vendor', 'vendor_name',
            'voucher', 'bill_no', 'bill_date', 'total', 'igst', 'igst_taxes',
            'cgst', 'cgst_taxes', 'sgst', 'sgst_taxes', 'note', 'products', 'created_at'
        ]
        read_only_fields = ['id', 'created_at', 'vendor_name', 'selected_bill_name', 'products']


class ExpenseBillUploadSerializer(serializers.Serializer):
    """Serializer for multiple file upload"""
    files = serializers.ListField(
        child=serializers.FileField(allow_empty_file=False),
        help_text="List of files to upload (PDF, JPG, PNG)"
    )
    file_type = serializers.ChoiceField(
        choices=TallyExpenseBill.BillType.choices,
        default=TallyExpenseBill.BillType.SINGLE
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


class ExpenseBillAnalysisRequestSerializer(serializers.Serializer):
    """Serializer for expense bill analysis request"""
    bill_id = serializers.UUIDField(help_text="UUID of the expense bill to analyze")


class ExpenseBillVerificationSerializer(serializers.Serializer):
    """Serializer for expense bill verification data"""
    vendor_id = serializers.UUIDField(required=False, allow_null=True)
    voucher = serializers.CharField(max_length=255, required=False)
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


class ExpenseBillSyncRequestSerializer(serializers.Serializer):
    """Serializer for expense bill sync request"""
    bill_id = serializers.UUIDField(help_text="UUID of the expense bill to sync")


class ExpenseBillSyncResponseSerializer(serializers.Serializer):
    """Serializer for expense bill sync response data"""
    id = serializers.CharField()
    voucher = serializers.CharField()
    bill_no = serializers.CharField()
    bill_date = serializers.CharField(allow_null=True)
    total = serializers.FloatField()
    name = serializers.CharField()
    company = serializers.CharField()
    gst_in = serializers.CharField()
    DR_LEDGER = serializers.ListField(child=serializers.DictField())
    CR_LEDGER = serializers.ListField(child=serializers.DictField())
    note = serializers.CharField()

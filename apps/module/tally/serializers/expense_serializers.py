import logging

from rest_framework import serializers

from apps.common.serializers import UploadedByUserSerializer  # noqa: F401
from .bill_base import (
    BaseTallyBillSerializer,
    BaseBillUploadSerializer,
    BillAnalysisRequestSerializer as _BillAnalysisRequestSerializer,  # noqa: F401
    BillSyncRequestSerializer,  # noqa: F401 - re-export
    BaseBillDetailSerializer,
)
from ..models import TallyExpenseBill, TallyExpenseAnalyzedBill, TallyExpenseAnalyzedProduct

logger = logging.getLogger(__name__)


# Alias for expense-specific naming
ExpenseBillAnalysisRequestSerializer = _BillAnalysisRequestSerializer


class TallyExpenseBillSerializer(BaseTallyBillSerializer):
    """Tally expense bill list serializer"""

    class Meta:
        model = TallyExpenseBill
        fields = BaseTallyBillSerializer.base_fields
        read_only_fields = BaseTallyBillSerializer.base_read_only_fields


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

    def get_consolidate_prod_data(self, obj):
        """Get consolidated product data if exists"""
        try:
            # Ensure obj is a TallyExpenseAnalyzedBill instance
            if not hasattr(obj, 'consolidated_products'):
                logger.warning(f"Object {type(obj)} does not have consolidated_products attribute")
                return []
                
            consolidated_products = obj.consolidated_products.all()
            from ..models import TallyExpenseConsolidatedProduct

            return [{
                'id': str(consolidated_product.id),
                'item_details': consolidated_product.item_details,
                'chart_of_accounts': str(consolidated_product.chart_of_accounts.name) if consolidated_product.chart_of_accounts else "No COA Ledger",
                'amount': float(consolidated_product.amount or 0),
                'debit_or_credit': consolidated_product.debit_or_credit or "debit",
                'original_entries_count': consolidated_product.original_entries_count or 0,
                'consolidation_notes': consolidated_product.consolidation_notes or "",
                'created_at': consolidated_product.created_at.isoformat() if consolidated_product.created_at else None
            } for consolidated_product in consolidated_products]
        except Exception as e:
            logger.error(f"Error getting consolidated products for {type(obj)}: {str(e)}")
            return []

    def to_representation(self, instance):
        """Override to include consolidate_prod array like Zoho pattern"""
        try:
            data = super().to_representation(instance)

            # Add consolidate_prod array (matching Zoho pattern for frontend compatibility)
            consolidated_data = self.get_consolidate_prod_data(instance)
            data['consolidate_prod'] = consolidated_data

            return data
        except Exception as e:
            logger.error(f"Error in TallyExpenseAnalyzedBillSerializer.to_representation for {type(instance)}: {str(e)}")
            # Return basic data without consolidated_products if there's an error
            data = super().to_representation(instance)
            data['consolidate_prod'] = []
            return data

    class Meta:
        model = TallyExpenseAnalyzedBill
        fields = [
            'id', 'selected_bill', 'selected_bill_name', 'vendor', 'vendor_name',
            'voucher', 'bill_no', 'bill_date', 'due_date', 'total', 'vendor_amount', 'vendor_debit_or_credit',
            'igst', 'igst_taxes', 'igst_debit_or_credit', 'cgst', 'cgst_taxes', 'cgst_debit_or_credit',
            'sgst', 'sgst_taxes', 'sgst_debit_or_credit', 'tds', 'tds_taxes', 'tds_debit_or_credit',
            'other_adjustment', 'other_adjustment_taxes', 'other_adjustment_debit_or_credit',
            'note', 'consolidate', 'products', 'created_at'
        ]
        read_only_fields = ['id', 'created_at', 'vendor_name', 'selected_bill_name', 'products']


class ExpenseBillUploadSerializer(BaseBillUploadSerializer):
    """Serializer for expense bill file upload"""
    bill_model = TallyExpenseBill
    
    file_type = serializers.ChoiceField(
        choices=TallyExpenseBill.BillType.choices,
        default=TallyExpenseBill.BillType.SINGLE,
        help_text="Type: SINGLE (each file is separate bill) or MULTI (PDF pages split)"
    )


# ExpenseBillAnalysisRequestSerializer is aliased at top from bill_base


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
    tds = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, default=0)
    other_adjustment = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, default=0)
    igst_taxes_id = serializers.UUIDField(required=False, allow_null=True)
    cgst_taxes_id = serializers.UUIDField(required=False, allow_null=True)
    sgst_taxes_id = serializers.UUIDField(required=False, allow_null=True)
    tds_taxes_id = serializers.UUIDField(required=False, allow_null=True)
    other_adjustment_taxes_id = serializers.UUIDField(required=False, allow_null=True)

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


# ExpenseBillSyncRequestSerializer - use BillSyncRequestSerializer from bill_base (re-exported above)


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


class TallyExpenseBillDetailSerializer(BaseBillDetailSerializer):
    """Enhanced Tally expense bill serializer with analyzed data"""
    
    analyzed_bill_model = TallyExpenseAnalyzedBill
    
    def get_analyzed_bill(self, obj):
        """Get analyzed bill data if exists"""
        try:
            analyzed_bill = TallyExpenseAnalyzedBill.objects.filter(selected_bill=obj).first()
            if analyzed_bill:
                return TallyExpenseAnalyzedBillSerializer(analyzed_bill).data
        except Exception as e:
            logger.error(f"Error getting analyzed bill for {obj.id}: {str(e)}")
        return None

    class Meta:
        model = TallyExpenseBill
        fields = BaseBillDetailSerializer.base_fields
        read_only_fields = BaseBillDetailSerializer.base_read_only_fields

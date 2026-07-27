import logging

from rest_framework import serializers

from apps.common.serializers import UploadedByUserSerializer  # noqa: F401
from .bill_base import (
    SafeDecimalField,
    BaseTallyBillSerializer,
    BaseBillUploadSerializer,
    BillAnalysisRequestSerializer,  # noqa: F401 - re-export
    BillSyncRequestSerializer,  # noqa: F401 - re-export
    BaseBillDetailSerializer,
)
from ..models import TallyVendorBill, TallyVendorAnalyzedBill, TallyVendorAnalyzedProduct, Ledger

logger = logging.getLogger(__name__)


class TallyVendorBillSerializer(BaseTallyBillSerializer):
    """Tally vendor bill list serializer"""

    class Meta:
        model = TallyVendorBill
        fields = BaseTallyBillSerializer.base_fields
        read_only_fields = BaseTallyBillSerializer.base_read_only_fields


class TallyVendorAnalyzedProductSerializer(serializers.ModelSerializer):
    taxes_name = serializers.CharField(source='taxes.name', read_only=True)

    # Use SafeDecimalField for all decimal fields that might have invalid values
    price = SafeDecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)
    amount = SafeDecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)
    igst = SafeDecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)
    cgst = SafeDecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)
    sgst = SafeDecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)

    cgst_ledger_name = serializers.CharField(source='cgst_ledger.name', read_only=True)
    sgst_ledger_name = serializers.CharField(source='sgst_ledger.name', read_only=True)
    igst_ledger_name = serializers.CharField(source='igst_ledger.name', read_only=True)

    class Meta:
        model = TallyVendorAnalyzedProduct
        fields = [
            'id', 'item_name', 'item_details', 'taxes', 'taxes_name',
            'price', 'quantity', 'amount', 'product_gst',
            'igst', 'cgst', 'sgst',
            'cgst_ledger', 'cgst_ledger_name',
            'sgst_ledger', 'sgst_ledger_name',
            'igst_ledger', 'igst_ledger_name',
            'created_at',
        ]
        read_only_fields = [
            'id', 'created_at', 'taxes_name',
            'cgst_ledger_name', 'sgst_ledger_name', 'igst_ledger_name',
        ]


class TallyVendorAnalyzedBillSerializer(serializers.ModelSerializer):
    products = TallyVendorAnalyzedProductSerializer(many=True, read_only=True)
    vendor_name = serializers.CharField(source='vendor.name', read_only=True)
    selected_bill_name = serializers.CharField(source='selected_bill.bill_munshi_name', read_only=True)

    # Use SafeDecimalField for all decimal fields that might have invalid values
    total = SafeDecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)
    igst = SafeDecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)
    cgst = SafeDecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)
    sgst = SafeDecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)
    discount = SafeDecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)
    cess = SafeDecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)
    freight = SafeDecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)

    def get_consolidated_product(self, obj):
        """Get consolidated product data as array for verification flexibility (like Zoho)"""
        try:
            # Ensure obj is a TallyVendorAnalyzedBill instance
            if not hasattr(obj, 'consolidated_products'):
                logger.warning(f"Object {type(obj)} does not have consolidated_products attribute")
                return []
                
            consolidated_products = obj.consolidated_products.all()
            from ..models import TallyVendorConsolidatedProduct

            # Return as array for consistency with Zoho pattern (frontend expects consolidate_prod array)
            return [{
                'id': str(consolidated_product.id),
                'item_name': consolidated_product.item_name,
                'item_details': consolidated_product.item_details,
                'tax_ledger': str(consolidated_product.taxes.name) if consolidated_product.taxes else "No Tax Ledger",
                'price': float(consolidated_product.price or 0),
                'rate': float(consolidated_product.price or 0),  # Alias for price
                'quantity': consolidated_product.quantity or 1,
                'amount': float(consolidated_product.amount or 0),
                'product_gst': consolidated_product.product_gst or "",
                'igst': float(consolidated_product.igst or 0),
                'cgst': float(consolidated_product.cgst or 0),
                'sgst': float(consolidated_product.sgst or 0),
                'original_items_count': consolidated_product.original_items_count or 0,
                'consolidation_notes': consolidated_product.consolidation_notes or "",
                'created_at': consolidated_product.created_at.isoformat() if consolidated_product.created_at else None
            } for consolidated_product in consolidated_products]
        except Exception as e:
            logger.error(f"Error getting consolidated products for {type(obj)}: {str(e)}")
            return []  # Return empty array if no consolidated products exist

    def to_representation(self, instance):
        """Override to include consolidate_prod array like Zoho pattern"""
        try:
            data = super().to_representation(instance)

            # Add consolidate_prod array (matching Zoho pattern for frontend compatibility)
            consolidated_data = self.get_consolidated_product(instance)
            data['consolidate_prod'] = consolidated_data

            return data
        except Exception as e:
            logger.error(f"Error in TallyVendorAnalyzedBillSerializer.to_representation for {type(instance)}: {str(e)}")
            # Return basic data without consolidated_products if there's an error
            data = super().to_representation(instance)
            data['consolidate_prod'] = []
            return data

    class Meta:
        model = TallyVendorAnalyzedBill
        fields = [
            'id', 'selected_bill', 'selected_bill_name', 'vendor', 'vendor_name',
            'bill_no', 'bill_date', 'due_date', 'total', 'igst', 'igst_taxes',
            'cgst', 'cgst_taxes', 'sgst', 'sgst_taxes', 'discount', 'discount_taxes',
            'cess', 'cess_taxes', 'freight', 'freight_taxes',
            # round_off was missing → saved values dropped on reload; UI
            # showed blank amount + blank ledger even after successful save.
            'round_off', 'round_off_taxes', 'round_off_debit_or_credit',
            'gst_type', 'note', 'consolidate', 'products', 'created_at'
        ]
        read_only_fields = ['id', 'created_at', 'vendor_name', 'selected_bill_name', 'products']


class VendorBillUploadSerializer(BaseBillUploadSerializer):
    """Serializer for vendor bill file upload"""
    bill_model = TallyVendorBill
    
    file_type = serializers.ChoiceField(
        choices=TallyVendorBill.BillType.choices,
        default=TallyVendorBill.BillType.SINGLE,
        help_text="Type: SINGLE (each file is separate bill) or MULTI (PDF pages split)"
    )


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
    cess = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, default=0)
    cess_taxes_id = serializers.UUIDField(required=False, allow_null=True)
    freight = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, default=0)
    freight_taxes_id = serializers.UUIDField(required=False, allow_null=True)

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


# BillSyncRequestSerializer is re-exported from bill_base


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


class TallyVendorBillDetailSerializer(BaseBillDetailSerializer):
    """Enhanced Tally vendor bill serializer with analyzed data"""
    
    analyzed_bill_model = TallyVendorAnalyzedBill
    # We override get_analyzed_bill to use the correct serializer

    def get_analyzed_bill(self, obj):
        """Get analyzed bill data if exists"""
        try:
            analyzed_bill = TallyVendorAnalyzedBill.objects.filter(selected_bill=obj).first()
            if analyzed_bill:
                try:
                    return TallyVendorAnalyzedBillSerializer(analyzed_bill).data
                except Exception as e:
                    logger.error(f"Error serializing analyzed bill {analyzed_bill.id}: {str(e)}")
                    return None
        except Exception as e:
            logger.error(f"Error getting analyzed bill for {obj.id}: {str(e)}")
        return None

    class Meta:
        model = TallyVendorBill
        fields = BaseBillDetailSerializer.base_fields
        read_only_fields = BaseBillDetailSerializer.base_read_only_fields

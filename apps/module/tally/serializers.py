# apps/module/tally/serializers.py
from rest_framework import serializers
from .models import (
    Ledger, ParentLedger, TallyConfig, StockItem,
    TallyVendorBill, TallyVendorAnalyzedBill, TallyVendorAnalyzedProduct, TallyVendorConsolidatedProduct,
    TallyExpenseBill, TallyExpenseAnalyzedBill, TallyExpenseAnalyzedProduct, TallyExpenseConsolidatedProduct
)


class ParentLedgerSerializer(serializers.ModelSerializer):
    class Meta:
        model = ParentLedger
        fields = ['id', 'parent', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']


class LedgerSerializer(serializers.ModelSerializer):
    parent_name = serializers.CharField(source='parent.parent', read_only=True)

    class Meta:
        model = Ledger
        fields = [
            'id', 'master_id', 'alter_id', 'name', 'parent', 'parent_name',
            'alias', 'opening_balance', 'gst_in', 'company', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class StockItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = StockItem
        fields = [
            'id', 'master_id', 'alter_id', 'name', 'parent', 'unit',
            'category', 'gst_applicable', 'item_code', 'alias', 'company',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class StockItemBulkCreateSerializer(serializers.Serializer):
    """
    Serializer for bulk creation of stock items from Tally data format.
    Expects: {"STOCKITEM": [{"Name": "...", "Master_Id": "...", ...}, ...]}
    """
    STOCKITEM = StockItemSerializer(many=True)


class TallyConfigSerializer(serializers.ModelSerializer):
    # Read-only fields for displaying parent names in the frontend
    igst_parent_names = serializers.SerializerMethodField()
    cgst_parent_names = serializers.SerializerMethodField()
    sgst_parent_names = serializers.SerializerMethodField()
    vendor_parent_names = serializers.SerializerMethodField()
    coa_parent_names = serializers.SerializerMethodField()
    expense_coa_parent_names = serializers.SerializerMethodField()
    tds_parent_names = serializers.SerializerMethodField()
    payment_parent_names = serializers.SerializerMethodField()

    class Meta:
        model = TallyConfig
        fields = [
            'id', 'tally_product_allow_sync', 'igst_parents', 'cgst_parents', 'sgst_parents',
            'vendor_parents', 'chart_of_accounts_parents', 'chart_of_accounts_expense_parents',
            'tds_parents', 'payment_parents',
            'igst_parent_names', 'cgst_parent_names', 'sgst_parent_names',
            'vendor_parent_names', 'coa_parent_names', 'expense_coa_parent_names',
            'tds_parent_names', 'payment_parent_names'
        ]
        read_only_fields = ['id', 'igst_parent_names', 'cgst_parent_names', 'sgst_parent_names',
                           'vendor_parent_names', 'coa_parent_names', 'expense_coa_parent_names',
                           'tds_parent_names', 'payment_parent_names']

    def validate_igst_parents(self, value):
        # Skip validation - let Django handle it at the database level
        return value

    def validate_cgst_parents(self, value):
        # Skip validation - let Django handle it at the database level
        return value

    def validate_sgst_parents(self, value):
        # Skip validation - let Django handle it at the database level
        return value

    def validate_vendor_parents(self, value):
        # Skip validation - let Django handle it at the database level
        return value

    def validate_chart_of_accounts_parents(self, value):
        # Skip validation - let Django handle it at the database level
        return value

    def validate_chart_of_accounts_expense_parents(self, value):
        # Skip validation - let Django handle it at the database level
        return value

    def validate_tds_parents(self, value):
        # Skip validation - let Django handle it at the database level
        return value

    def validate_payment_parents(self, value):
        # Skip validation - let Django handle it at the database level
        return value

    def get_igst_parent_names(self, obj):
        return [parent.parent for parent in obj.igst_parents.all() if parent.parent]

    def get_cgst_parent_names(self, obj):
        return [parent.parent for parent in obj.cgst_parents.all() if parent.parent]

    def get_sgst_parent_names(self, obj):
        return [parent.parent for parent in obj.sgst_parents.all() if parent.parent]

    def get_vendor_parent_names(self, obj):
        return [parent.parent for parent in obj.vendor_parents.all() if parent.parent]

    def get_coa_parent_names(self, obj):
        return [parent.parent for parent in obj.chart_of_accounts_parents.all() if parent.parent]

    def get_expense_coa_parent_names(self, obj):
        return [parent.parent for parent in obj.chart_of_accounts_expense_parents.all() if parent.parent]

    def get_tds_parent_names(self, obj):
        return [parent.parent for parent in obj.tds_parents.all() if parent.parent]

    def get_payment_parent_names(self, obj):
        return [parent.parent for parent in obj.payment_parents.all() if parent.parent]


class LedgerBulkCreateSerializer(serializers.Serializer):
    """
    Serializer for bulk creation of ledgers from Tally data format.
    Expects: {"LEDGER": [{"Name": "...", "Master_Id": "...", ...}, ...]}
    """
    LEDGER = LedgerSerializer(many=True)


class TallyVendorBillSerializer(serializers.ModelSerializer):
    uploaded_by_username = serializers.CharField(source='uploaded_by.username', read_only=True)
    organization_name = serializers.CharField(source='organization.name', read_only=True)
    bill_belong_your_org = serializers.SerializerMethodField()
    description = serializers.SerializerMethodField()

    class Meta:
        model = TallyVendorBill
        fields = [
            'id', 'bill_munshi_name', 'file', 'file_type', 'analysed_data',
            'status', 'process', 'uploaded_by', 'uploaded_by_username',
            'organization_name', 'bill_belong_your_org', 'description', 
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'uploaded_by_username', 'organization_name']

    def get_bill_belong_your_org(self, obj):
        """Get bill ownership status - calculate if not set"""
        if obj.bill_belong_your_org is not None:
            return obj.bill_belong_your_org
        
        # Calculate on-the-fly if not set and analysed_data exists
        if obj.analysed_data and obj.organization:
            belongs_to_org, _ = self._validate_ownership(obj.analysed_data, obj.organization)
            return belongs_to_org
        
        return False

    def get_description(self, obj):
        """Get ownership description - calculate if not set"""
        if obj.description:
            return obj.description
        
        # Calculate on-the-fly if not set and analysed_data exists
        if obj.analysed_data and obj.organization:
            _, description = self._validate_ownership(obj.analysed_data, obj.organization)
            return description
        
        return "Not analyzed yet"

    def _validate_ownership(self, json_data, organization):
        """Private method to validate ownership"""
        try:
            from_data = json_data.get('from', {})
            if isinstance(from_data, dict):
                vendor_name = from_data.get('name', '').strip()
                vendor_gst = from_data.get('gst_number', '').strip()
            else:
                vendor_name = ''
                vendor_gst = ''

            # GST number match
            if vendor_gst and organization.gst_number:
                if vendor_gst.replace(' ', '').upper() == organization.gst_number.replace(' ', '').upper():
                    return True, f"GST number match: {vendor_gst}"

            # Company name match
            if vendor_name and organization.name:
                org_name_clean = organization.name.lower().strip()
                vendor_name_clean = vendor_name.lower().strip()
                
                if org_name_clean == vendor_name_clean:
                    return True, f"Exact company name match: {vendor_name}"
                
                if org_name_clean in vendor_name_clean or vendor_name_clean in org_name_clean:
                    return True, f"Partial company name match: {vendor_name}"
            
            return False, f"No match found. Vendor: {vendor_name}, GST: {vendor_gst}"
            
        except Exception as e:
            return False, f"Validation error: {str(e)}"


class TallyExpenseBillSerializer(serializers.ModelSerializer):
    uploaded_by_username = serializers.CharField(source='uploaded_by.username', read_only=True)
    organization_name = serializers.CharField(source='organization.name', read_only=True)
    bill_belong_your_org = serializers.SerializerMethodField()
    description = serializers.SerializerMethodField()

    class Meta:
        model = TallyExpenseBill
        fields = [
            'id', 'bill_munshi_name', 'file', 'file_type', 'analysed_data',
            'status', 'process', 'uploaded_by', 'uploaded_by_username',
            'organization_name', 'bill_belong_your_org', 'description', 
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'uploaded_by_username', 'organization_name']

    def get_bill_belong_your_org(self, obj):
        """Get bill ownership status - calculate if not set"""
        if obj.bill_belong_your_org is not None:
            return obj.bill_belong_your_org
        
        # Calculate on-the-fly if not set and analysed_data exists
        if obj.analysed_data and obj.organization:
            belongs_to_org, _ = self._validate_ownership(obj.analysed_data, obj.organization)
            return belongs_to_org
        
        return False

    def get_description(self, obj):
        """Get ownership description - calculate if not set"""
        if obj.description:
            return obj.description
        
        # Calculate on-the-fly if not set and analysed_data exists
        if obj.analysed_data and obj.organization:
            _, description = self._validate_ownership(obj.analysed_data, obj.organization)
            return description
        
        return "Not analyzed yet"

    def _validate_ownership(self, json_data, organization):
        """Private method to validate ownership"""
        try:
            from_data = json_data.get('from', {})
            if isinstance(from_data, dict):
                vendor_name = from_data.get('name', '').strip()
                vendor_gst = from_data.get('gst_number', '').strip()
            else:
                vendor_name = ''
                vendor_gst = ''

            # GST number match
            if vendor_gst and organization.gst_number:
                if vendor_gst.replace(' ', '').upper() == organization.gst_number.replace(' ', '').upper():
                    return True, f"GST number match: {vendor_gst}"

            # Company name match
            if vendor_name and organization.name:
                org_name_clean = organization.name.lower().strip()
                vendor_name_clean = vendor_name.lower().strip()
                
                if org_name_clean == vendor_name_clean:
                    return True, f"Exact company name match: {vendor_name}"
                
                if org_name_clean in vendor_name_clean or vendor_name_clean in org_name_clean:
                    return True, f"Partial company name match: {vendor_name}"
            
            return False, f"No match found. Vendor: {vendor_name}, GST: {vendor_gst}"
            
        except Exception as e:
            return False, f"Validation error: {str(e)}"
# TALLY VENDOR ANALYZED BILL SERIALIZERS
# =====================================================

class TallyVendorAnalyzedProductSerializer(serializers.ModelSerializer):
    """Serializer for Tally vendor analyzed products"""

    class Meta:
        model = TallyVendorAnalyzedProduct
        fields = [
            'id', 'item_name', 'item_details', 'taxes', 'price', 'quantity',
            'amount', 'product_gst', 'igst', 'cgst', 'sgst', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']


class TallyVendorConsolidatedProductSerializer(serializers.ModelSerializer):
    """Serializer for Tally vendor consolidated products - matches analyzed product structure"""

    class Meta:
        model = TallyVendorConsolidatedProduct
        fields = [
            'id', 'item_name', 'item_details', 'taxes', 'price', 'quantity',
            'amount', 'product_gst', 'igst', 'cgst', 'sgst', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']


class TallyVendorAnalyzedBillSerializer(serializers.ModelSerializer):
    """Serializer for Tally vendor analyzed bills with consolidated product support"""

    products = TallyVendorAnalyzedProductSerializer(many=True, read_only=True)

    def to_representation(self, instance):
        """Add consolidated product data to response"""
        data = super().to_representation(instance)

        # Always include consolidated product data if it exists (regardless of consolidate flag)
        # Return as array for frontend verification flexibility
        try:
            consolidated_products = instance.consolidated_products.all()
            consolidated_serializer = TallyVendorConsolidatedProductSerializer(consolidated_products, many=True)
            data['consolidate_prod'] = consolidated_serializer.data
        except:
            # No consolidated products exist, return empty array
            data['consolidate_prod'] = []

        return data

    class Meta:
        model = TallyVendorAnalyzedBill
        fields = [
            'id', 'selected_bill', 'vendor', 'bill_no', 'bill_date', 'due_date',
            'total', 'igst', 'cgst', 'sgst', 'igst_taxes', 'cgst_taxes', 'sgst_taxes',
            'discount', 'discount_taxes',
            'note', 'gst_type', 'consolidate', 'created_at', 'products'
        ]
        read_only_fields = ['id', 'created_at', 'products']


# =====================================================
# TALLY EXPENSE ANALYZED BILL SERIALIZERS
# =====================================================

class TallyExpenseAnalyzedProductSerializer(serializers.ModelSerializer):
    """Serializer for Tally expense analyzed products"""

    class Meta:
        model = TallyExpenseAnalyzedProduct
        fields = [
            'id', 'item_details', 'chart_of_accounts', 'amount',
            'debit_or_credit', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']


class TallyExpenseConsolidatedProductSerializer(serializers.ModelSerializer):
    """Serializer for Tally expense consolidated products - matches analyzed product structure"""

    class Meta:
        model = TallyExpenseConsolidatedProduct
        fields = [
            'id', 'item_details', 'chart_of_accounts', 'amount',
            'debit_or_credit', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']


class TallyExpenseAnalyzedBillSerializer(serializers.ModelSerializer):
    """Serializer for Tally expense analyzed bills with consolidated product support"""

    products = TallyExpenseAnalyzedProductSerializer(many=True, read_only=True)

    def to_representation(self, instance):
        """Add consolidated product data to response"""
        data = super().to_representation(instance)

        # Always include consolidated product data if it exists (regardless of consolidate flag)
        # Return as array for frontend verification flexibility
        try:
            consolidated_product = instance.consolidated_product
            consolidated_serializer = TallyExpenseConsolidatedProductSerializer(consolidated_product)
            data['consolidate_prod'] = [consolidated_serializer.data]
        except TallyExpenseConsolidatedProduct.DoesNotExist:
            # No consolidated product exists, return empty array
            data['consolidate_prod'] = []

        return data

    class Meta:
        model = TallyExpenseAnalyzedBill
        fields = [
            'id', 'selected_bill', 'vendor', 'bill_no', 'bill_date', 'voucher', 'due_date',
            'total', 'igst', 'cgst', 'sgst', 'tds', 'igst_taxes', 'cgst_taxes', 'sgst_taxes', 'tds_taxes',
            'vendor_amount', 'vendor_debit_or_credit', 'igst_debit_or_credit',
            'cgst_debit_or_credit', 'sgst_debit_or_credit', 'tds_debit_or_credit',
            'other_adjustment', 'other_adjustment_taxes', 'other_adjustment_debit_or_credit',
            'note', 'gst_type', 'consolidate', 'created_at', 'products'
        ]
        read_only_fields = ['id', 'created_at', 'products']


# =====================================================
# ENHANCED MAIN BILL SERIALIZERS WITH ANALYZED DATA
# =====================================================

class TallyVendorBillDetailSerializer(serializers.ModelSerializer):
    """Enhanced Tally vendor bill serializer with analyzed data"""

    uploaded_by_username = serializers.CharField(source='uploaded_by.username', read_only=True)
    organization_name = serializers.CharField(source='organization.name', read_only=True)
    analyzed_bill = serializers.SerializerMethodField()
    next_bill = serializers.SerializerMethodField()

    def get_analyzed_bill(self, obj):
        """Get analyzed bill data if exists"""
        try:
            from .models import TallyVendorAnalyzedBill
            analyzed_bill = TallyVendorAnalyzedBill.objects.filter(selected_bill=obj).first()
            if analyzed_bill:
                return TallyVendorAnalyzedBillSerializer(analyzed_bill).data
        except:
            pass
        return None

    def get_next_bill(self, obj):
        """Get next bill to process"""
        next_bills = TallyVendorBill.objects.filter(
            organization=obj.organization,
            status=TallyVendorBill.BillStatus.ANALYSED
        ).exclude(id=obj.id).values_list('id', flat=True)

        if next_bills:
            import random
            return str(random.choice(list(next_bills)))
        return None

    class Meta:
        model = TallyVendorBill
        fields = [
            'id', 'bill_munshi_name', 'file', 'file_type', 'analysed_data',
            'status', 'process', 'uploaded_by', 'uploaded_by_username',
            'organization_name', 'created_at', 'updated_at', 'analyzed_bill', 'next_bill'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'uploaded_by_username', 'organization_name', 'analyzed_bill', 'next_bill']


class TallyExpenseBillDetailSerializer(serializers.ModelSerializer):
    """Enhanced Tally expense bill serializer with analyzed data"""

    uploaded_by_username = serializers.CharField(source='uploaded_by.username', read_only=True)
    organization_name = serializers.CharField(source='organization.name', read_only=True)
    analyzed_bill = serializers.SerializerMethodField()
    next_bill = serializers.SerializerMethodField()

    def get_analyzed_bill(self, obj):
        """Get analyzed bill data if exists"""
        try:
            from .models import TallyExpenseAnalyzedBill
            analyzed_bill = TallyExpenseAnalyzedBill.objects.filter(selected_bill=obj).first()
            if analyzed_bill:
                return TallyExpenseAnalyzedBillSerializer(analyzed_bill).data
        except:
            pass
        return None

    def get_next_bill(self, obj):
        """Get next bill to process"""
        next_bills = TallyExpenseBill.objects.filter(
            organization=obj.organization,
            status=TallyExpenseBill.BillStatus.ANALYSED
        ).exclude(id=obj.id).values_list('id', flat=True)

        if next_bills:
            import random
            return str(random.choice(list(next_bills)))
        return None

    class Meta:
        model = TallyExpenseBill
        fields = [
            'id', 'bill_munshi_name', 'file', 'file_type', 'analysed_data',
            'status', 'process', 'uploaded_by', 'uploaded_by_username',
            'organization_name', 'created_at', 'updated_at', 'analyzed_bill', 'next_bill'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'uploaded_by_username', 'organization_name', 'analyzed_bill', 'next_bill']
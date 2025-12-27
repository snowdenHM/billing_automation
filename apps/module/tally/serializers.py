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
    
    class Meta:
        model = TallyConfig
        fields = [
            'id', 'tally_product_allow_sync', 'igst_parents', 'cgst_parents', 'sgst_parents',
            'vendor_parents', 'chart_of_accounts_parents', 'chart_of_accounts_expense_parents',
            'igst_parent_names', 'cgst_parent_names', 'sgst_parent_names', 
            'vendor_parent_names', 'coa_parent_names', 'expense_coa_parent_names'
        ]
        read_only_fields = ['id', 'igst_parent_names', 'cgst_parent_names', 'sgst_parent_names',
                           'vendor_parent_names', 'coa_parent_names', 'expense_coa_parent_names']
    
    def validate_igst_parents(self, value):
        return self._validate_parent_ledgers(value, 'igst_parents')
    
    def validate_cgst_parents(self, value):
        return self._validate_parent_ledgers(value, 'cgst_parents')
    
    def validate_sgst_parents(self, value):
        return self._validate_parent_ledgers(value, 'sgst_parents')
    
    def validate_vendor_parents(self, value):
        return self._validate_parent_ledgers(value, 'vendor_parents')
    
    def validate_chart_of_accounts_parents(self, value):
        return self._validate_parent_ledgers(value, 'chart_of_accounts_parents')
    
    def validate_chart_of_accounts_expense_parents(self, value):
        return self._validate_parent_ledgers(value, 'chart_of_accounts_expense_parents')
    
    def _validate_parent_ledgers(self, value, field_name):
        """Custom validation for parent ledger fields - forgiving validation like Node.js version"""
        if not value:
            return value
        
        print(f"Validating {field_name} with values: {value}")
        
        # Get organization from context
        organization = self.context.get('organization')
        request = self.context.get('request')
        view = self.context.get('view')
        
        # Try to get organization if not in context
        if not organization and view and hasattr(view, 'get_organization'):
            try:
                organization = view.get_organization()
            except Exception as e:
                print(f"Error getting organization: {e}")
        
        if not organization:
            print(f"No organization context for {field_name} validation - allowing all values")
            return value
        
        print(f"Validating {field_name} for organization: {organization.name} (ID: {organization.id})")
        
        # Convert values to string IDs
        parent_ids = []
        for item in value:
            if hasattr(item, 'id'):
                parent_ids.append(str(item.id))
            elif isinstance(item, str):
                parent_ids.append(item)
            else:
                parent_ids.append(str(item))
        
        if not parent_ids:
            return value
        
        try:
            from .models import ParentLedger
            
            # Check which ParentLedgers exist for this organization
            existing_parents = ParentLedger.objects.filter(
                id__in=parent_ids,
                organization=organization
            )
            
            existing_ids = set(str(parent.id) for parent in existing_parents)
            provided_ids = set(parent_ids)
            missing_ids = provided_ids - existing_ids
            
            print(f"Existing ParentLedger IDs: {existing_ids}")
            print(f"Provided ParentLedger IDs: {provided_ids}")
            print(f"Missing ParentLedger IDs: {missing_ids}")
            
            # Instead of raising an error, just warn and return valid IDs only
            if missing_ids:
                print(f"WARNING: Some ParentLedger IDs don't exist for organization {organization.name}: {missing_ids}")
                # Return only the valid IDs that exist
                valid_parent_objects = []
                for pid in parent_ids:
                    if pid in existing_ids:
                        # Find the actual ParentLedger object
                        parent_obj = existing_parents.filter(id=pid).first()
                        if parent_obj:
                            valid_parent_objects.append(parent_obj)
                
                print(f"Returning {len(valid_parent_objects)} valid ParentLedger objects")
                return valid_parent_objects
            
        except Exception as e:
            print(f"Error in ParentLedger validation: {e}")
            # Don't fail validation on database errors - just return original value
            return value
        
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


class LedgerBulkCreateSerializer(serializers.Serializer):
    """
    Serializer for bulk creation of ledgers from Tally data format.
    Expects: {"LEDGER": [{"Name": "...", "Master_Id": "...", ...}, ...]}
    """
    LEDGER = LedgerSerializer(many=True)


class TallyVendorBillSerializer(serializers.ModelSerializer):
    uploaded_by_username = serializers.CharField(source='uploaded_by.username', read_only=True)
    organization_name = serializers.CharField(source='organization.name', read_only=True)

    class Meta:
        model = TallyVendorBill
        fields = [
            'id', 'bill_munshi_name', 'file', 'file_type', 'analysed_data',
            'status', 'process', 'uploaded_by', 'uploaded_by_username',
            'organization_name', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'uploaded_by_username', 'organization_name']


class TallyExpenseBillSerializer(serializers.ModelSerializer):
    uploaded_by_username = serializers.CharField(source='uploaded_by.username', read_only=True)
    organization_name = serializers.CharField(source='organization.name', read_only=True)

    class Meta:
        model = TallyExpenseBill
        fields = [
            'id', 'bill_munshi_name', 'file', 'file_type', 'analysed_data',
            'status', 'process', 'uploaded_by', 'uploaded_by_username',
            'organization_name', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'uploaded_by_username', 'organization_name']


# =====================================================
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
            'id', 'selected_bill', 'vendor', 'bill_no', 'bill_date', 'voucher',
            'total', 'igst', 'cgst', 'sgst', 'tds', 'igst_taxes', 'cgst_taxes', 'sgst_taxes', 'tds_taxes',
            'vendor_amount', 'vendor_debit_or_credit', 'igst_debit_or_credit',
            'cgst_debit_or_credit', 'sgst_debit_or_credit', 'tds_debit_or_credit', 'note', 'gst_type',
            'consolidate', 'created_at', 'products'
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

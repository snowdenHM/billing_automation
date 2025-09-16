# apps/module/tally/serializers.py
from rest_framework import serializers
from .models import Ledger, ParentLedger, TallyConfig, StockItem


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
    class Meta:
        model = TallyConfig
        fields = [
            'id', 'igst_parents', 'cgst_parents', 'sgst_parents',
            'vendor_parents', 'chart_of_accounts_parents', 'chart_of_accounts_expense_parents'
        ]
        read_only_fields = ['id']


class LedgerBulkCreateSerializer(serializers.Serializer):
    """
    Serializer for bulk creation of ledgers from Tally data format.
    Expects: {"LEDGER": [{"Name": "...", "Master_Id": "...", ...}, ...]}
    """
    LEDGER = LedgerSerializer(many=True)

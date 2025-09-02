from rest_framework import serializers
from ..models import Ledger, ParentLedger


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
            'alias', 'opening_balance', 'gst_in', 'company',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'parent_name']

    def to_representation(self, instance):
        """Customize the output format to match the expected response"""
        data = super().to_representation(instance)
        # Add parent name for easier consumption
        if instance.parent:
            data['parent_name'] = instance.parent.parent
        return data


class LedgerBulkCreateSerializer(serializers.Serializer):
    """
    Serializer for bulk creating ledgers from Tally data format
    Expected format: {"LEDGER": [{"Master_Id": "...", "Name": "...", ...}, ...]}
    """
    LEDGER = serializers.ListField(
        child=serializers.DictField(),
        help_text="List of ledger objects to create"
    )

    def validate_LEDGER(self, value):
        """Validate the LEDGER list format"""
        if not value:
            raise serializers.ValidationError("LEDGER list cannot be empty")

        required_fields = ['Name']  # At minimum, we need a name
        for idx, ledger_data in enumerate(value):
            for field in required_fields:
                if field not in ledger_data or not ledger_data[field]:
                    raise serializers.ValidationError(
                        f"Ledger at index {idx} is missing required field: {field}"
                    )

        return value

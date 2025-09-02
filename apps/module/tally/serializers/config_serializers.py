from rest_framework import serializers
from drf_spectacular.utils import extend_schema_field
from typing import List
from ..models import TallyConfig


class TallyConfigSerializer(serializers.ModelSerializer):
    # Read-only fields for displaying parent ledger names
    igst_parent_names = serializers.SerializerMethodField()
    cgst_parent_names = serializers.SerializerMethodField()
    sgst_parent_names = serializers.SerializerMethodField()
    vendor_parent_names = serializers.SerializerMethodField()
    coa_parent_names = serializers.SerializerMethodField()
    expense_coa_parent_names = serializers.SerializerMethodField()

    class Meta:
        model = TallyConfig
        fields = [
            'id', 'igst_parents', 'cgst_parents', 'sgst_parents',
            'vendor_parents', 'chart_of_accounts_parents',
            'chart_of_accounts_expense_parents',
            'igst_parent_names', 'cgst_parent_names', 'sgst_parent_names',
            'vendor_parent_names', 'coa_parent_names', 'expense_coa_parent_names'
        ]
        read_only_fields = ['id']

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_igst_parent_names(self, obj) -> List[str]:
        return [parent.parent for parent in obj.igst_parents.all()]

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_cgst_parent_names(self, obj) -> List[str]:
        return [parent.parent for parent in obj.cgst_parents.all()]

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_sgst_parent_names(self, obj) -> List[str]:
        return [parent.parent for parent in obj.sgst_parents.all()]

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_vendor_parent_names(self, obj) -> List[str]:
        return [parent.parent for parent in obj.vendor_parents.all()]

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_coa_parent_names(self, obj) -> List[str]:
        return [parent.parent for parent in obj.chart_of_accounts_parents.all()]

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_expense_coa_parent_names(self, obj) -> List[str]:
        return [parent.parent for parent in obj.chart_of_accounts_expense_parents.all()]

    def validate(self, data):
        """Ensure all parent ledgers belong to the same organization"""
        organization = self.context['request'].user.memberships.first().organization

        all_parents = []
        for field_name in ['igst_parents', 'cgst_parents', 'sgst_parents',
                          'vendor_parents', 'chart_of_accounts_parents',
                          'chart_of_accounts_expense_parents']:
            if field_name in data:
                all_parents.extend(data[field_name])

        for parent in all_parents:
            if parent.organization != organization:
                raise serializers.ValidationError(
                    f"Parent ledger '{parent.parent}' does not belong to your organization"
                )

        return data

from rest_framework import serializers
from drf_spectacular.utils import extend_schema_field
from typing import List
from ..models import TallyConfig, ParentLedger


class TallyConfigSerializer(serializers.ModelSerializer):
    # Write-only fields for accepting parent ledger UUIDs during creation/update
    igst_parents = serializers.PrimaryKeyRelatedField(
        queryset=ParentLedger.objects.all(), many=True, write_only=True, required=False
    )
    cgst_parents = serializers.PrimaryKeyRelatedField(
        queryset=ParentLedger.objects.all(), many=True, write_only=True, required=False
    )
    sgst_parents = serializers.PrimaryKeyRelatedField(
        queryset=ParentLedger.objects.all(), many=True, write_only=True, required=False
    )
    vendor_parents = serializers.PrimaryKeyRelatedField(
        queryset=ParentLedger.objects.all(), many=True, write_only=True, required=False
    )
    chart_of_accounts_parents = serializers.PrimaryKeyRelatedField(
        queryset=ParentLedger.objects.all(), many=True, write_only=True, required=False
    )
    chart_of_accounts_expense_parents = serializers.PrimaryKeyRelatedField(
        queryset=ParentLedger.objects.all(), many=True, write_only=True, required=False
    )

    # Read-only fields for displaying parent ledger names in response
    igst_parent_names = serializers.SerializerMethodField()
    cgst_parent_names = serializers.SerializerMethodField()
    sgst_parent_names = serializers.SerializerMethodField()
    vendor_parent_names = serializers.SerializerMethodField()
    coa_parent_names = serializers.SerializerMethodField()
    expense_coa_parent_names = serializers.SerializerMethodField()

    class Meta:
        model = TallyConfig
        fields = [
            'id',
            # Write-only UUID fields for input
            'igst_parents', 'cgst_parents', 'sgst_parents',
            'vendor_parents', 'chart_of_accounts_parents', 'chart_of_accounts_expense_parents',
            # Read-only name fields for output
            'igst_parent_names', 'cgst_parent_names', 'sgst_parent_names',
            'vendor_parent_names', 'coa_parent_names', 'expense_coa_parent_names'
        ]
        read_only_fields = ['id']

    def to_representation(self, instance):
        """Override to only return name fields in the response, not UUIDs"""
        data = super().to_representation(instance)
        # Remove UUID fields from response
        uuid_fields = ['igst_parents', 'cgst_parents', 'sgst_parents',
                      'vendor_parents', 'chart_of_accounts_parents', 'chart_of_accounts_expense_parents']
        for field in uuid_fields:
            data.pop(field, None)
        return data

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
        # Get organization from the request context
        request = self.context.get('request')
        organization = None

        # Try to get organization from different sources
        if hasattr(request, 'organization'):
            organization = request.organization
        elif hasattr(request.user, 'memberships') and request.user.is_authenticated:
            membership = request.user.memberships.first()
            if membership:
                organization = membership.organization

        if not organization:
            return data  # Skip validation if no organization context

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

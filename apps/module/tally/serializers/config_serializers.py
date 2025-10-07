from rest_framework import serializers
from drf_spectacular.utils import extend_schema_field
from typing import List
from ..models import TallyConfig, ParentLedger


class TallyConfigSerializer(serializers.ModelSerializer):
    # Use PrimaryKeyRelatedField for ManyToMany relationships to avoid serialization issues
    igst_parents = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=ParentLedger.objects.none(),  # Will be set in __init__
        required=False
    )
    cgst_parents = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=ParentLedger.objects.none(),
        required=False
    )
    sgst_parents = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=ParentLedger.objects.none(),
        required=False
    )
    vendor_parents = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=ParentLedger.objects.none(),
        required=False
    )
    chart_of_accounts_parents = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=ParentLedger.objects.none(),
        required=False
    )
    chart_of_accounts_expense_parents = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=ParentLedger.objects.none(),
        required=False
    )

    # Read-only fields for displaying parent ledger names in response
    igst_parent_names = serializers.SerializerMethodField()
    cgst_parent_names = serializers.SerializerMethodField()
    sgst_parent_names = serializers.SerializerMethodField()
    vendor_parent_names = serializers.SerializerMethodField()
    coa_parent_names = serializers.SerializerMethodField()
    expense_coa_parent_names = serializers.SerializerMethodField()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Set the queryset based on the organization from context or instance
        if hasattr(self, 'context') and 'request' in self.context:
            request = self.context['request']
            if hasattr(request, 'user') and hasattr(request.user, 'organization'):
                org_queryset = ParentLedger.objects.filter(organization=request.user.organization)
            else:
                org_queryset = ParentLedger.objects.none()
        elif self.instance and hasattr(self.instance, 'organization'):
            org_queryset = ParentLedger.objects.filter(organization=self.instance.organization)
        else:
            org_queryset = ParentLedger.objects.none()

        # Set queryset for all ManyToMany fields
        for field_name in ['igst_parents', 'cgst_parents', 'sgst_parents',
                          'vendor_parents', 'chart_of_accounts_parents',
                          'chart_of_accounts_expense_parents']:
            if field_name in self.fields:
                self.fields[field_name].queryset = org_queryset

    class Meta:
        model = TallyConfig
        fields = [
            'id',
            # ManyToMany fields - now properly handled with PrimaryKeyRelatedField
            'igst_parents', 'cgst_parents', 'sgst_parents',
            'vendor_parents', 'chart_of_accounts_parents', 'chart_of_accounts_expense_parents',
            # Read-only name fields for output
            'igst_parent_names', 'cgst_parent_names', 'sgst_parent_names',
            'vendor_parent_names', 'coa_parent_names', 'expense_coa_parent_names'
        ]
        read_only_fields = ['id', 'igst_parent_names', 'cgst_parent_names', 'sgst_parent_names',
                           'vendor_parent_names', 'coa_parent_names', 'expense_coa_parent_names']

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

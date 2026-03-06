from __future__ import annotations

import logging

from rest_framework import serializers

from apps.common.serializers import FileUploadField, OrgField, UploadedByUserSerializer  # noqa: F401
from apps.module.zoho.models import (
    ExpenseBill,
    ExpenseZohoBill,
    ExpenseZohoProduct,
    ExpenseZohoConsolidatedProduct,
    ZohoVendor,
    ZohoChartOfAccount,
    ZohoTaxes,
)
from .bill_base import (
    OrgLookupMixin,
    BaseZohoBillListSerializer,
    BaseZohoBillDetailSerializer,
    BaseZohoBillUploadSerializer,
    BaseZohoBillMultipleUploadSerializer,
)

logger = logging.getLogger(__name__)


# ---------- Product serializers ----------

class ExpenseZohoProductSerializer(serializers.ModelSerializer):
    """Serializer for Expense product line items."""

    class Meta:
        model = ExpenseZohoProduct
        fields = [
            "id", "zohoBill", "item_details", "amount", "chart_of_accounts", "taxes", "created_at"
        ]
        read_only_fields = ["id", "zohoBill", "created_at"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get('request') if hasattr(self, 'context') else None
        if request and hasattr(request, 'organization'):
            organization = request.organization
            self.fields['chart_of_accounts'].queryset = ZohoChartOfAccount.objects.filter(organization=organization)
            self.fields['taxes'].queryset = ZohoTaxes.objects.filter(organization=organization)
        # Also try organization from context directly (used by nested re-serialization)
        elif hasattr(self, 'context') and 'organization' in self.context:
            organization = self.context['organization']
            self.fields['chart_of_accounts'].queryset = ZohoChartOfAccount.objects.filter(organization=organization)
            self.fields['taxes'].queryset = ZohoTaxes.objects.filter(organization=organization)


class ExpenseZohoConsolidatedProductSerializer(OrgLookupMixin, serializers.ModelSerializer):
    """Serializer for consolidated expense product."""

    item_details = serializers.CharField(source='consolidated_item_details', read_only=True)
    amount = serializers.DecimalField(source='consolidated_amount', max_digits=15, decimal_places=2, read_only=True)
    zohoBill = serializers.UUIDField(source='zohoBill.id', read_only=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        organization = self._get_organization()
        if organization:
            self.fields['chart_of_accounts'].queryset = ZohoChartOfAccount.objects.filter(
                organization=organization
            )
            self.fields['taxes'].queryset = ZohoTaxes.objects.filter(
                organization=organization
            )

    class Meta:
        model = ExpenseZohoConsolidatedProduct
        fields = [
            "id", "zohoBill", "item_details", "amount", "chart_of_accounts", "taxes",
            "created_at"
        ]
        read_only_fields = ["id", "zohoBill", "created_at"]


# ---------- ZohoBill (nested detail) serializer ----------

class ExpenseZohoBillSerializer(OrgLookupMixin, serializers.ModelSerializer):
    """Serializer for Expense Zoho bill with consolidated product support."""

    products = ExpenseZohoProductSerializer(many=True, read_only=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        organization = self._get_organization()
        if organization:
            self.fields['vendor'].queryset = ZohoVendor.objects.filter(organization=organization)
            self.fields['chart_of_accounts'].queryset = ZohoChartOfAccount.objects.filter(organization=organization)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        organization = self._get_organization()
        if organization and instance:
            ctx = {**(self.context or {}), 'organization': organization}
            data['products'] = ExpenseZohoProductSerializer(
                instance.products.all(), many=True, context=ctx,
            ).data
            consolidated_products = instance.consolidated_products.all()
            if consolidated_products.exists():
                data['consolidate_prod'] = ExpenseZohoConsolidatedProductSerializer(
                    consolidated_products, many=True, context=ctx,
                ).data
            else:
                data['consolidate_prod'] = []
        return data

    class Meta:
        model = ExpenseZohoBill
        fields = [
            "id", "selectBill", "vendor", "bill_no", "bill_date", "due_date", "total",
            "chart_of_accounts", "igst", "cgst", "sgst", "note", "consolidate",
            "created_at", "products",
        ]
        read_only_fields = ["id", "selectBill", "created_at"]


# ---------- Listing / detail / upload (shared bases) ----------

class ZohoExpenseBillSerializer(BaseZohoBillListSerializer):
    class Meta(BaseZohoBillListSerializer.Meta):
        model = ExpenseBill
        ref_name = "ZohoExpenseBill"


class ZohoExpenseBillDetailSerializer(BaseZohoBillDetailSerializer):
    zoho_bill_serializer_class = ExpenseZohoBillSerializer

    class Meta:
        ref_name = "ZohoExpenseBillDetail"


class ZohoExpenseBillUploadSerializer(BaseZohoBillUploadSerializer):
    class Meta(BaseZohoBillUploadSerializer.Meta):
        model = ExpenseBill
        ref_name = "ZohoExpenseBillUploadRequest"


class ZohoExpenseBillMultipleUploadSerializer(BaseZohoBillMultipleUploadSerializer):
    class Meta:
        ref_name = "ZohoExpenseBillMultipleUploadRequest"


# ---------- Verification serializers (expense-specific) ----------

class ZohoExpenseVerifyProductItemSerializer(serializers.Serializer):
    """Edits to each product during Expense verification."""

    id = serializers.UUIDField()
    chart_of_accounts = serializers.PrimaryKeyRelatedField(
        queryset=ZohoChartOfAccount.objects.all(), required=False, allow_null=True
    )
    taxes = serializers.PrimaryKeyRelatedField(
        queryset=ZohoTaxes.objects.all(), required=False, allow_null=True
    )
    item_details = serializers.CharField(required=False, allow_blank=True)
    amount = serializers.CharField(required=False, allow_blank=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        organization = self.context.get('organization') if hasattr(self, 'context') else None
        if organization:
            self.fields['chart_of_accounts'].queryset = ZohoChartOfAccount.objects.filter(organization=organization)
            self.fields['taxes'].queryset = ZohoTaxes.objects.filter(organization=organization)


class ZohoExpenseBillVerifySerializer(serializers.Serializer):
    """Verification payload for the analysed Expense bill header + products."""

    vendor = serializers.PrimaryKeyRelatedField(
        queryset=ZohoVendor.objects.all(), required=False, allow_null=True
    )
    note = serializers.CharField(required=False, allow_blank=True)
    bill_no = serializers.CharField(required=False, allow_blank=True)
    bill_date = serializers.DateField(required=False, allow_null=True)
    due_date = serializers.DateField(required=False, allow_null=True)
    cgst = serializers.CharField(required=False, allow_blank=True)
    sgst = serializers.CharField(required=False, allow_blank=True)
    igst = serializers.CharField(required=False, allow_blank=True)
    total = serializers.CharField(required=False, allow_blank=True)
    products = ZohoExpenseVerifyProductItemSerializer(many=True, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        organization = self.context.get('organization') if hasattr(self, 'context') else None
        if organization:
            self.fields['vendor'].queryset = ZohoVendor.objects.filter(organization=organization)

    class Meta:
        ref_name = "ZohoExpenseBillVerify"

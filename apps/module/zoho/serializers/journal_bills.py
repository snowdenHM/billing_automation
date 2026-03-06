from __future__ import annotations

import logging

from rest_framework import serializers

from apps.common.serializers import FileUploadField, OrgField, UploadedByUserSerializer  # noqa: F401
from apps.module.zoho.models import (
    JournalBill,
    JournalZohoBill,
    JournalZohoProduct,
    JournalZohoConsolidatedProduct,
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

class JournalZohoProductSerializer(serializers.ModelSerializer):
    """Serializer for Journal product line items."""

    class Meta:
        model = JournalZohoProduct
        fields = [
            "id", "zohoBill", "item_details", "chart_of_accounts",
            "amount", "debit_or_credit", "created_at"
        ]
        read_only_fields = ["id", "zohoBill", "created_at"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Try context dict first, then request
        organization = None
        if hasattr(self, 'context'):
            organization = self.context.get('organization')
            if not organization:
                request = self.context.get('request')
                if request and hasattr(request, 'organization'):
                    organization = request.organization
        if organization:
            self.fields['chart_of_accounts'].queryset = ZohoChartOfAccount.objects.filter(organization=organization)


class JournalZohoConsolidatedProductSerializer(OrgLookupMixin, serializers.ModelSerializer):
    """Serializer for consolidated journal product."""

    item_details = serializers.CharField(source='consolidated_item_details', read_only=True)
    amount = serializers.DecimalField(source='consolidated_amount', max_digits=15, decimal_places=2, read_only=True)
    zohoBill = serializers.UUIDField(source='zohoBill.id', read_only=True)
    debit_or_credit = serializers.CharField(default="debit", read_only=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        organization = self._get_organization()
        if organization:
            self.fields['chart_of_accounts'].queryset = ZohoChartOfAccount.objects.filter(
                organization=organization
            )

    class Meta:
        model = JournalZohoConsolidatedProduct
        fields = [
            "id", "zohoBill", "item_details", "amount", "chart_of_accounts", "debit_or_credit",
            "created_at"
        ]
        read_only_fields = ["id", "zohoBill", "created_at"]


# ---------- ZohoBill (nested detail) serializer ----------

class JournalZohoBillSerializer(OrgLookupMixin, serializers.ModelSerializer):
    """Serializer for Journal Zoho bill with consolidated product support."""

    products = JournalZohoProductSerializer(many=True, read_only=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        organization = self._get_organization()
        if organization:
            self.fields['vendor'].queryset = ZohoVendor.objects.filter(organization=organization)
            coa_fields = ['vendor_coa', 'igst_coa', 'cgst_coa', 'sgst_coa']
            for field in coa_fields:
                if field in self.fields:
                    self.fields[field].queryset = ZohoChartOfAccount.objects.filter(organization=organization)

    def validate(self, attrs):
        """Validate chart of account fields belong to the organization."""
        organization = self.context.get('organization')
        if not organization:
            return attrs

        chart_account_fields = {
            'vendor_coa': 'vendor chart of account',
            'igst_coa': 'IGST chart of account',
            'cgst_coa': 'CGST chart of account',
            'sgst_coa': 'SGST chart of account',
        }
        for field_name, field_display in chart_account_fields.items():
            field_value = attrs.get(field_name)
            if field_value and not ZohoChartOfAccount.objects.filter(
                id=field_value.id, organization=organization
            ).exists():
                raise serializers.ValidationError({
                    field_name: f"The selected {field_display} does not belong to this organization. "
                                "Please sync chart of accounts from Zoho Books first."
                })
        return attrs

    def to_representation(self, instance):
        data = super().to_representation(instance)
        organization = self._get_organization()
        if organization and instance:
            ctx = {**(self.context or {}), 'organization': organization}
            data['products'] = JournalZohoProductSerializer(
                instance.products.all(), many=True, context=ctx,
            ).data
            consolidated_products = instance.consolidated_products.all()
            if consolidated_products.exists():
                data['consolidate_prod'] = JournalZohoConsolidatedProductSerializer(
                    consolidated_products, many=True, context=ctx,
                ).data
            else:
                data['consolidate_prod'] = []
        return data

    class Meta:
        model = JournalZohoBill
        fields = [
            "id", "selectBill", "vendor", "bill_no", "bill_date", "due_date",
            "vendor_coa", "vendor_debit_or_credit", "vendor_amount",
            "total", "igst", "igst_coa", "igst_debit_or_credit",
            "cgst", "cgst_coa", "cgst_debit_or_credit",
            "sgst", "sgst_coa", "sgst_debit_or_credit",
            "note", "consolidate", "created_at", "products",
        ]
        read_only_fields = ["id", "selectBill", "created_at"]


# ---------- Listing / detail / upload (shared bases) ----------

class ZohoJournalBillSerializer(BaseZohoBillListSerializer):
    class Meta(BaseZohoBillListSerializer.Meta):
        model = JournalBill
        ref_name = "ZohoJournalBill"


class ZohoJournalBillDetailSerializer(BaseZohoBillDetailSerializer):
    zoho_bill_serializer_class = JournalZohoBillSerializer

    class Meta:
        ref_name = "ZohoJournalBillDetail"


class ZohoJournalBillUploadSerializer(BaseZohoBillUploadSerializer):
    class Meta(BaseZohoBillUploadSerializer.Meta):
        model = JournalBill
        ref_name = "ZohoJournalBillUploadRequest"


class ZohoJournalBillMultipleUploadSerializer(BaseZohoBillMultipleUploadSerializer):
    class Meta:
        ref_name = "ZohoJournalBillMultipleUploadRequest"


# ---------- Verification serializers (journal-specific) ----------

class ZohoJournalVerifyProductItemSerializer(serializers.Serializer):
    """Edits to each product during Journal verification."""

    id = serializers.UUIDField()
    chart_of_accounts = serializers.PrimaryKeyRelatedField(
        queryset=ZohoChartOfAccount.objects.all(), required=False, allow_null=True
    )
    item_details = serializers.CharField(required=False, allow_blank=True)
    amount = serializers.CharField(required=False, allow_blank=True)
    debit_or_credit = serializers.ChoiceField(
        choices=[("credit", "Credit"), ("debit", "Debit")],
        required=False, default="credit",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        organization = self.context.get('organization') if hasattr(self, 'context') else None
        if organization:
            self.fields['chart_of_accounts'].queryset = ZohoChartOfAccount.objects.filter(organization=organization)


class ZohoJournalBillVerifySerializer(serializers.Serializer):
    """Verification payload for the analysed Journal bill header + products."""

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
    products = ZohoJournalVerifyProductItemSerializer(many=True, required=False)

    class Meta:
        ref_name = "ZohoJournalBillVerify"

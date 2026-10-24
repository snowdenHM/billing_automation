"""Serializers for Zoho sales invoices.

Mirror of ``vendor_bills.py`` with the party swapped from vendor to
customer; the shared bases in ``bill_base.py`` carry everything that is
genuinely common.
"""
from __future__ import annotations

import logging

from rest_framework import serializers

from apps.common.serializers import FileUploadField, UploadedByUserSerializer  # noqa: F401
from apps.module.zoho.models import (
    SalesBill,
    SalesZohoBill,
    SalesZohoProduct,
    SalesZohoConsolidatedProduct,
    ZohoChartOfAccount,
    ZohoCustomer,
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

class SalesZohoProductSerializer(OrgLookupMixin, serializers.ModelSerializer):
    """A single invoice line item."""

    class Meta:
        model = SalesZohoProduct
        fields = [
            "id", "zohoBill", "item_name", "item_details", "chart_of_accounts",
            "taxes", "rate", "quantity", "amount", "created_at",
        ]
        read_only_fields = ["id", "zohoBill", "created_at"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        organization = self._get_organization()
        if organization:
            self.fields['chart_of_accounts'].queryset = ZohoChartOfAccount.objects.filter(organization=organization)
            self.fields['taxes'].queryset = ZohoTaxes.objects.filter(organization=organization)


class SalesZohoConsolidatedProductSerializer(OrgLookupMixin, serializers.ModelSerializer):
    """The single rolled-up line item used when ``consolidate`` is on."""

    item_name = serializers.CharField(source='consolidated_item_name', read_only=True)
    item_details = serializers.CharField(source='consolidated_item_details', read_only=True)
    amount = serializers.DecimalField(source='consolidated_amount', max_digits=15, decimal_places=2, read_only=True)
    rate = serializers.DecimalField(source='consolidated_rate', max_digits=15, decimal_places=2, read_only=True)
    quantity = serializers.DecimalField(source='total_quantity', max_digits=12, decimal_places=2, read_only=True)
    zohoBill = serializers.UUIDField(source='zohoBill.id', read_only=True)

    class Meta:
        model = SalesZohoConsolidatedProduct
        fields = [
            "id", "zohoBill", "item_name", "item_details", "rate", "quantity",
            "amount", "chart_of_accounts", "taxes", "original_entries_count",
            "created_at",
        ]
        read_only_fields = ["id", "zohoBill", "original_entries_count", "created_at"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        organization = self._get_organization()
        if organization:
            self.fields['chart_of_accounts'].queryset = ZohoChartOfAccount.objects.filter(organization=organization)
            self.fields['taxes'].queryset = ZohoTaxes.objects.filter(organization=organization)


# ---------- Analysed invoice header ----------

class SalesZohoBillSerializer(OrgLookupMixin, serializers.ModelSerializer):
    """The analysed invoice header; also the write target of the verify view."""

    products = SalesZohoProductSerializer(many=True, read_only=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        organization = self._get_organization()
        if organization:
            self.fields['customer'].queryset = ZohoCustomer.objects.filter(organization=organization)
            self.fields['discount_account'].queryset = ZohoChartOfAccount.objects.filter(organization=organization)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        organization = self._get_organization()
        if organization and instance:
            ctx = {**(self.context or {}), 'organization': organization}
            data['products'] = SalesZohoProductSerializer(
                instance.products.all(), many=True, context=ctx,
            ).data
            consolidated = instance.consolidated_products.all()
            data['consolidate_prod'] = SalesZohoConsolidatedProductSerializer(
                consolidated, many=True, context=ctx,
            ).data if consolidated.exists() else []
        return data

    class Meta:
        model = SalesZohoBill
        fields = [
            "id", "selectBill", "customer", "bill_no", "bill_date", "due_date",
            "total", "discount_type", "discount", "discount_amount", "discount_account",
            "adjustment_amount", "adjustment_description",
            "igst", "cgst", "sgst", "place_of_supply", "note", "consolidate",
            "created_at", "products",
        ]
        read_only_fields = ["id", "selectBill", "created_at"]


# ---------- Listing / detail / upload ----------

class ZohoSalesBillSerializer(BaseZohoBillListSerializer):
    class Meta(BaseZohoBillListSerializer.Meta):
        model = SalesBill
        fields = BaseZohoBillListSerializer.Meta.fields + ["zoho_invoice_id"]
        read_only_fields = BaseZohoBillListSerializer.Meta.read_only_fields + ["zoho_invoice_id"]
        ref_name = "ZohoSalesBill"


class ZohoSalesBillDetailSerializer(BaseZohoBillDetailSerializer):
    zoho_bill_serializer_class = SalesZohoBillSerializer

    class Meta:
        ref_name = "ZohoSalesBillDetail"


class ZohoSalesBillUploadSerializer(BaseZohoBillUploadSerializer):
    class Meta(BaseZohoBillUploadSerializer.Meta):
        model = SalesBill
        ref_name = "ZohoSalesBillUploadRequest"


class ZohoSalesBillMultipleUploadSerializer(BaseZohoBillMultipleUploadSerializer):
    class Meta:
        ref_name = "ZohoSalesBillMultipleUploadRequest"

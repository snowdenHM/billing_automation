from __future__ import annotations

from rest_framework import serializers

from apps.common.serializers import FileUploadField, OrgField, UploadedByUserSerializer  # noqa: F401
from apps.module.zoho.models import (
    VendorBill,
    VendorZohoBill,
    VendorZohoProduct,
    VendorZohoConsolidatedProduct,
    ZohoVendor,
    ZohoTdsTcs,
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


# ---------- Product serializers ----------

class VendorZohoProductSerializer(OrgLookupMixin, serializers.ModelSerializer):
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
        model = VendorZohoProduct
        fields = [
            "id", "zohoBill", "item_name", "item_details", "chart_of_accounts",
            "taxes", "reverse_charge_tax_id", "itc_eligibility", "rate",
            "quantity", "amount", "created_at"
        ]
        read_only_fields = ["id", "zohoBill", "created_at"]


class VendorZohoConsolidatedProductSerializer(OrgLookupMixin, serializers.ModelSerializer):
    """Serializer for consolidated product when consolidate=True."""

    item_name = serializers.CharField(source='consolidated_item_name', read_only=True)
    item_details = serializers.CharField(source='consolidated_item_details', read_only=True)
    rate = serializers.DecimalField(source='consolidated_rate', max_digits=15, decimal_places=2, read_only=True)
    quantity = serializers.DecimalField(source='total_quantity', max_digits=12, decimal_places=2, read_only=True)
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
        model = VendorZohoConsolidatedProduct
        fields = [
            "id", "zohoBill", "item_name", "item_details", "chart_of_accounts",
            "taxes", "reverse_charge_tax_id", "itc_eligibility", "rate",
            "quantity", "amount", "created_at"
        ]
        read_only_fields = ["id", "zohoBill", "created_at"]


# ---------- ZohoBill (nested detail) serializer ----------

class VendorZohoBillSerializer(OrgLookupMixin, serializers.ModelSerializer):
    products = VendorZohoProductSerializer(many=True, read_only=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        organization = self._get_organization()
        if organization:
            self.fields['vendor'].queryset = ZohoVendor.objects.filter(organization=organization)
            self.fields['tds_tcs_id'].queryset = ZohoTdsTcs.objects.filter(organization=organization)
            self.fields['discount_account'].queryset = ZohoChartOfAccount.objects.filter(organization=organization)

    # --- discount calculation helpers ---

    @staticmethod
    def _calculate_subtotal(instance):
        from decimal import Decimal
        subtotal = Decimal("0")
        for product in instance.products.all():
            try:
                amount = Decimal(str(product.amount)) if product.amount else Decimal("0")
                subtotal += amount
            except Exception:
                pass
        return subtotal

    @staticmethod
    def _calculate_discount_amount(discount, discount_type, subtotal):
        from decimal import Decimal
        if not discount or discount == 0:
            return Decimal("0")
        discount_value = Decimal(str(discount))
        if discount_type == "Percentage":
            return (subtotal * discount_value) / Decimal("100")
        return discount_value

    def update(self, instance, validated_data):
        discount = validated_data.get('discount', instance.discount)
        discount_type = validated_data.get('discount_type', instance.discount_type)
        subtotal = self._calculate_subtotal(instance)
        if discount is not None:
            validated_data['discount_amount'] = self._calculate_discount_amount(
                discount, discount_type, subtotal
            )
        return super().update(instance, validated_data)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        organization = self._get_organization()
        if organization and instance:
            ctx = {**(self.context or {}), 'organization': organization}
            data['products'] = VendorZohoProductSerializer(
                instance.products.all(), many=True, context=ctx,
            ).data
            consolidated_products = instance.consolidated_products.all()
            if consolidated_products.exists():
                data['consolidate_prod'] = VendorZohoConsolidatedProductSerializer(
                    consolidated_products, many=True, context=ctx,
                ).data
            else:
                data['consolidate_prod'] = []
        return data

    class Meta:
        model = VendorZohoBill
        fields = [
            "id", "selectBill", "vendor", "bill_no", "bill_date", "due_date", "total",
            "discount_type", "discount", "discount_amount", "discount_account",
            "adjustment_amount", "adjustment_description",
            "igst", "cgst", "sgst", "tds_tcs_id", "is_tax", "note", "consolidate",
            "created_at", "products",
        ]
        read_only_fields = ["id", "selectBill", "created_at", "discount_amount"]


# ---------- Listing / detail / upload (shared bases) ----------

class ZohoVendorBillSerializer(BaseZohoBillListSerializer):
    class Meta(BaseZohoBillListSerializer.Meta):
        model = VendorBill
        ref_name = "ZohoVendorBill"


class ZohoVendorBillDetailSerializer(BaseZohoBillDetailSerializer):
    zoho_bill_serializer_class = VendorZohoBillSerializer

    class Meta:
        ref_name = "ZohoVendorBillDetail"


class ZohoVendorBillUploadSerializer(BaseZohoBillUploadSerializer):
    class Meta(BaseZohoBillUploadSerializer.Meta):
        model = VendorBill
        ref_name = "ZohoVendorBillUploadRequest"


class ZohoVendorBillMultipleUploadSerializer(BaseZohoBillMultipleUploadSerializer):
    class Meta:
        ref_name = "ZohoVendorBillMultipleUploadRequest"


# ---------- Verification serializers (vendor-specific) ----------

class ZohoVerifyProductItemSerializer(serializers.Serializer):
    """Edits to each product during verification."""

    id = serializers.UUIDField()
    chart_of_accounts = serializers.PrimaryKeyRelatedField(
        queryset=ZohoChartOfAccount.objects.all(), required=False, allow_null=True
    )
    taxes = serializers.PrimaryKeyRelatedField(
        queryset=ZohoTaxes.objects.all(), required=False, allow_null=True
    )
    reverse_charge_tax_id = serializers.BooleanField(required=False)
    itc_eligibility = serializers.ChoiceField(
        choices=("eligible", "ineligible_section17", "ineligible_others"), required=False
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if hasattr(self, 'context') and 'organization' in self.context:
            organization = self.context['organization']
            self.fields['chart_of_accounts'].queryset = ZohoChartOfAccount.objects.filter(organization=organization)
            self.fields['taxes'].queryset = ZohoTaxes.objects.filter(organization=organization)


class ZohoVendorBillVerifySerializer(serializers.Serializer):
    """Verification payload for the analysed bill header + products."""

    vendor = serializers.PrimaryKeyRelatedField(
        queryset=ZohoVendor.objects.all(), required=False, allow_null=True
    )
    note = serializers.CharField(required=False, allow_blank=True)
    bill_no = serializers.CharField(required=False, allow_blank=True)
    bill_date = serializers.DateField(required=False, allow_null=True)
    due_date = serializers.DateField(required=False, allow_null=True)
    discount_type = serializers.ChoiceField(
        choices=[("INR", "INR"), ("Percentage", "Percentage")],
        required=False, allow_null=True,
    )
    discount = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True,
        help_text="User-entered discount value (e.g., 5 for 5%% or 100 for INR 100)",
    )
    discount_amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True, read_only=True,
        help_text="Calculated discount amount in INR (auto-calculated)",
    )
    discount_account = serializers.PrimaryKeyRelatedField(
        queryset=ZohoChartOfAccount.objects.all(), required=False, allow_null=True
    )
    adjustment_amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True,
    )
    adjustment_description = serializers.CharField(required=False, allow_blank=True)
    cgst = serializers.CharField(required=False, allow_blank=True)
    sgst = serializers.CharField(required=False, allow_blank=True)
    igst = serializers.CharField(required=False, allow_blank=True)
    tax_type = serializers.ChoiceField(choices=("TDS", "TCS", "No"), required=False, default="No")
    tds_tcs_id = serializers.PrimaryKeyRelatedField(
        queryset=ZohoTdsTcs.objects.all(), required=False, allow_null=True
    )
    products = ZohoVerifyProductItemSerializer(many=True, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if hasattr(self, 'context') and 'organization' in self.context:
            organization = self.context['organization']
            self.fields['vendor'].queryset = ZohoVendor.objects.filter(organization=organization)
            self.fields['tds_tcs_id'].queryset = ZohoTdsTcs.objects.filter(organization=organization)
            self.fields['discount_account'].queryset = ZohoChartOfAccount.objects.filter(organization=organization)

    def validate(self, attrs):
        tax_type = attrs.get("tax_type")
        tds_tcs = attrs.get("tds_tcs_id")
        if tax_type in ("TDS", "TCS") and not tds_tcs:
            raise serializers.ValidationError({"tds_tcs_id": "Required when tax_type is TDS or TCS."})
        return attrs

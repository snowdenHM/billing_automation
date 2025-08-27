from __future__ import annotations
from typing import List, Optional
from rest_framework import serializers

from apps.organizations.models import Organization
from apps.module.zoho.models import (
    VendorBill,
    VendorZohoBill,
    VendorZohoProduct,
    ZohoVendor,
    ZohoTdsTcs,
    ZohoChartOfAccount,
    ZohoTaxes,
)
from apps.module.zoho.serializers.base import (
    # use the shared one to avoid schema name collision
    SyncResultSerializer,  # noqa: F401  (imported for views that reference it)
)


class OrgField(serializers.PrimaryKeyRelatedField):
    def get_queryset(self):
        return Organization.objects.all()


# ---------- Read serializers ----------

class VendorZohoProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = VendorZohoProduct
        fields = [
            "id",
            "zohoBill",
            "item_name",
            "item_details",
            "chart_of_accounts",
            "taxes",
            "reverse_charge_tax_id",
            "itc_eligibility",
            "rate",
            "quantity",
            "amount",
            "created_at",
        ]
        read_only_fields = ["id", "zohoBill", "created_at"]


class VendorZohoBillSerializer(serializers.ModelSerializer):
    products = VendorZohoProductSerializer(many=True, read_only=True)

    class Meta:
        model = VendorZohoBill
        fields = [
            "id",
            "selectBill",
            "vendor",
            "bill_no",
            "bill_date",
            "total",
            "igst",
            "cgst",
            "sgst",
            "tds_tcs_id",
            "is_tax",
            "note",
            "created_at",
            "products",
        ]
        read_only_fields = ["id", "selectBill", "created_at"]


class VendorBillSerializer(serializers.ModelSerializer):
    organization = OrgField()
    analysed = serializers.BooleanField(source="process", read_only=True)

    class Meta:
        model = VendorBill
        fields = [
            "id",
            "organization",
            "billmunshiName",
            "file",
            "fileType",
            "analysed_data",
            "status",
            "process",
            "analysed",
            "created_at",
            "update_at",
        ]
        read_only_fields = ["id", "analysed_data", "status", "process", "created_at", "update_at"]


class VendorBillDetailSerializer(serializers.Serializer):
    bill = VendorBillSerializer()
    analysed = VendorZohoBillSerializer(allow_null=True)


# ---------- Write serializers ----------

class VendorBillUploadSerializer(serializers.ModelSerializer):
    """
    Multipart upload for a bill. Only file & organization are required.
    fileType controls split/merge behavior:
      - "Single Invoice/File": all PDF pages analysed as a single invoice
      - "Multiple Invoice/File": each PDF page becomes its own bill+analysis
    """
    organization = OrgField()

    class Meta:
        model = VendorBill
        fields = ["organization", "file", "fileType"]

    def validate(self, attrs):
        if not attrs.get("file"):
            raise serializers.ValidationError({"file": "A file is required."})
        return attrs


class VerifyProductItemSerializer(serializers.Serializer):
    """
    Edits to each product during verification.
    """
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


class VendorBillVerifySerializer(serializers.Serializer):
    """
    Verification payload for the analysed bill header + products.
    """
    vendor = serializers.PrimaryKeyRelatedField(queryset=ZohoVendor.objects.all(), required=False, allow_null=True)
    note = serializers.CharField(required=False, allow_blank=True)
    bill_no = serializers.CharField(required=False, allow_blank=True)
    bill_date = serializers.DateField(required=False, allow_null=True)
    cgst = serializers.CharField(required=False, allow_blank=True)
    sgst = serializers.CharField(required=False, allow_blank=True)
    igst = serializers.CharField(required=False, allow_blank=True)
    tax_type = serializers.ChoiceField(choices=("TDS", "TCS", "No"), required=False, default="No")
    tds_tcs_id = serializers.PrimaryKeyRelatedField(
        queryset=ZohoTdsTcs.objects.all(), required=False, allow_null=True
    )
    products = VerifyProductItemSerializer(many=True, required=False)

    def validate(self, attrs):
        tax_type = attrs.get("tax_type")
        tds_tcs = attrs.get("tds_tcs_id")
        if tax_type in ("TDS", "TCS") and not tds_tcs:
            raise serializers.ValidationError({"tds_tcs_id": "Required when tax_type is TDS or TCS."})
        return attrs


class UploadResultSerializer(serializers.Serializer):
    """
    Response for POST /vendor-bills/ to cover both single and multiple outcomes.
    """
    class Meta:
        ref_name = "VendorBillUploadResult"  # ensure unique component name

    created = serializers.IntegerField()
    bills = VendorBillSerializer(many=True)

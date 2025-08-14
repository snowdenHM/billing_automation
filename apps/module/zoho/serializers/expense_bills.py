from __future__ import annotations
from rest_framework import serializers

from apps.organizations.models import Organization
from apps.module.zoho.models import (
    ExpenseBill,
    ExpenseZohoBill,
    ExpenseZohoProduct,
    ZohoVendor,
    ZohoChartOfAccount,
)
from apps.module.zoho.serializers.base import SyncResultSerializer  # re-use shared type


class OrgField(serializers.PrimaryKeyRelatedField):
    def get_queryset(self):
        return Organization.objects.all()


# ---------- Read serializers ----------

class ExpenseZohoProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExpenseZohoProduct
        fields = [
            "id",
            "zohoBill",
            "item_details",
            "chart_of_accounts",
            "vendor",
            "amount",
            "debit_or_credit",
            "created_at",
        ]
        read_only_fields = ["id", "zohoBill", "created_at"]


class ExpenseZohoBillSerializer(serializers.ModelSerializer):
    # NOTE: 'products' is the reverse related_name on ExpenseZohoProduct. No 'source=' needed.
    products = ExpenseZohoProductSerializer(many=True, read_only=True)

    class Meta:
        model = ExpenseZohoBill
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
            "note",
            "created_at",
            "products",
        ]
        read_only_fields = ["id", "selectBill", "created_at"]


class ExpenseBillSerializer(serializers.ModelSerializer):
    organization = OrgField()
    analysed = serializers.BooleanField(source="process", read_only=True)

    class Meta:
        model = ExpenseBill
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
        ref_name = "ExpenseBillWithStatusEnum"



class ExpenseBillDetailSerializer(serializers.Serializer):
    bill = ExpenseBillSerializer()
    analysed = ExpenseZohoBillSerializer(allow_null=True)


# ---------- Write serializers ----------

class ExpenseBillUploadSerializer(serializers.ModelSerializer):
    """
    Multipart upload. fileType behavior:
      - "Single Invoice/File": analyse all PDF pages as ONE invoice
      - "Multiple Invoice/File": each PDF page becomes its own bill+analysis
    """
    organization = OrgField()

    class Meta:
        model = ExpenseBill
        fields = ["organization", "file", "fileType"]

    def validate(self, attrs):
        if not attrs.get("file"):
            raise serializers.ValidationError({"file": "A file is required."})
        return attrs


class VerifyExpenseProductItemSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    chart_of_accounts = serializers.PrimaryKeyRelatedField(
        queryset=ZohoChartOfAccount.objects.all(), required=False, allow_null=True
    )
    vendor = serializers.PrimaryKeyRelatedField(
        queryset=ZohoVendor.objects.all(), required=False, allow_null=True
    )
    amount = serializers.CharField(required=False, allow_blank=True)
    debit_or_credit = serializers.ChoiceField(choices=("credit", "debit"), required=False)


class ExpenseBillVerifySerializer(serializers.Serializer):
    vendor = serializers.PrimaryKeyRelatedField(queryset=ZohoVendor.objects.all(), required=False, allow_null=True)
    note = serializers.CharField(required=False, allow_blank=True)
    bill_no = serializers.CharField(required=False, allow_blank=True)
    bill_date = serializers.DateField(required=False, allow_null=True)
    cgst = serializers.CharField(required=False, allow_blank=True)
    sgst = serializers.CharField(required=False, allow_blank=True)
    igst = serializers.CharField(required=False, allow_blank=True)
    total = serializers.CharField(required=False, allow_blank=True)
    products = VerifyExpenseProductItemSerializer(many=True, required=False)


class ExpenseUploadResultSerializer(serializers.Serializer):
    class Meta:
        ref_name = "ExpenseBillUploadResult"  # unique name for schema

    created = serializers.IntegerField()
    bills = ExpenseBillSerializer(many=True)

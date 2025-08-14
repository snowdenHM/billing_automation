from __future__ import annotations

from rest_framework import serializers

from apps.module.tally.models import (
    TallyVendorBill,
    TallyVendorAnalyzedBill,
    TallyVendorAnalyzedProduct,
    TallyExpenseBill,
    TallyExpenseAnalyzedBill,
    TallyExpenseAnalyzedProduct,
    Ledger,
)


# ----------------------
# Common helpers/fields
# ----------------------

class LedgerPKField(serializers.PrimaryKeyRelatedField):
    def __init__(self, **kwargs):
        super().__init__(queryset=Ledger.objects.all(), **kwargs)


# ----------------------
# Vendor Bill Serializers
# ----------------------

class VendorBillUploadSerializer(serializers.ModelSerializer):
    class Meta:
        model = TallyVendorBill
        fields = ["file", "fileType"]
        ref_name = "TallyVendorBillUpload"   # <- unique name for schema


class VendorBillSerializer(serializers.ModelSerializer):
    class Meta:
        model = TallyVendorBill
        fields = [
            "id", "billmunshiName", "file", "fileType", "analysed_data",
            "status", "process", "created_at", "updated_at"
        ]
        read_only_fields = ["id", "billmunshiName", "analysed_data", "status", "process", "created_at", "updated_at"]
        ref_name = "TallyVendorBill"


class VendorAnalyzedProductSerializer(serializers.ModelSerializer):
    taxes = LedgerPKField(required=False, allow_null=True)

    class Meta:
        model = TallyVendorAnalyzedProduct
        fields = [
            "id", "item_name", "item_details", "taxes",
            "price", "quantity", "amount", "product_gst", "igst", "cgst", "sgst", "created_at"
        ]
        read_only_fields = ["id", "created_at"]
        ref_name = "TallyVendorAnalyzedProduct"


class VendorAnalyzedBillSerializer(serializers.ModelSerializer):
    vendor = LedgerPKField(required=False, allow_null=True)
    products = VendorAnalyzedProductSerializer(many=True, required=False)

    class Meta:
        model = TallyVendorAnalyzedBill
        fields = [
            "id", "selectBill", "vendor", "bill_no", "bill_date", "total",
            "igst", "igst_taxes", "cgst", "cgst_taxes", "sgst", "sgst_taxes",
            "gst_type", "note", "created_at", "products"
        ]
        read_only_fields = ["id", "created_at"]
        ref_name = "TallyVendorAnalyzedBill"


# ----------------------
# Expense Bill Serializers
# ----------------------

class ExpenseBillUploadSerializer(serializers.ModelSerializer):
    class Meta:
        model = TallyExpenseBill
        fields = ["file", "fileType"]
        ref_name = "TallyExpenseBillUpload"


class ExpenseBillSerializer(serializers.ModelSerializer):
    class Meta:
        model = TallyExpenseBill
        fields = [
            "id", "billmunshiName", "file", "fileType", "analysed_data",
            "status", "process", "created_at", "updated_at"
        ]
        read_only_fields = ["id", "billmunshiName", "analysed_data", "status", "process", "created_at", "updated_at"]
        ref_name = "TallyExpenseBill"


class ExpenseAnalyzedProductSerializer(serializers.ModelSerializer):
    chart_of_accounts = LedgerPKField(required=False, allow_null=True)

    class Meta:
        model = TallyExpenseAnalyzedProduct
        fields = ["id", "item_details", "chart_of_accounts", "amount", "debit_or_credit", "created_at"]
        read_only_fields = ["id", "created_at"]
        ref_name = "TallyExpenseAnalyzedProduct"


class ExpenseAnalyzedBillSerializer(serializers.ModelSerializer):
    vendor = LedgerPKField(required=False, allow_null=True)
    products = ExpenseAnalyzedProductSerializer(many=True, required=False)

    class Meta:
        model = TallyExpenseAnalyzedBill
        fields = [
            "id", "selectBill", "vendor", "voucher", "bill_no", "bill_date", "total",
            "igst", "igst_taxes", "cgst", "cgst_taxes", "sgst", "sgst_taxes",
            "note", "created_at", "products"
        ]
        read_only_fields = ["id", "created_at"]
        ref_name = "TallyExpenseAnalyzedBill"


# ----------------------
# Small success payloads
# ----------------------

class VendorSyncResultSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    status = serializers.ChoiceField(choices=["Verified", "Synced"])

    class Meta:
        ref_name = "TallyVendorSyncResult"


class ExpenseSyncResultSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    status = serializers.ChoiceField(choices=["Verified", "Synced"])

    class Meta:
        ref_name = "TallyExpenseSyncResult"

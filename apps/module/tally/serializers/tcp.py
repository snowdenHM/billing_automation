# apps/module/tally/serializers/tcp.py
from __future__ import annotations

from typing import Any, List

from rest_framework import serializers

from apps.module.tally.models import (
    Ledger,
    ParentLedger,
    TallyExpenseAnalyzedBill,
    TallyExpenseAnalyzedProduct,
    TallyVendorAnalyzedBill,
    TallyVendorAnalyzedProduct,
)


# -------- Ledgers --------

class ParentLedgerSerializer(serializers.ModelSerializer):
    class Meta:
        model = ParentLedger
        fields = ["id", "parent", "created_at", "update_at"]
        read_only_fields = ["id", "created_at", "update_at"]


class LedgerSerializer(serializers.ModelSerializer):
    parent_name = serializers.CharField(write_only=True, required=False, allow_blank=True)
    parent = ParentLedgerSerializer(read_only=True)

    class Meta:
        model = Ledger
        fields = [
            "id",
            "master_id",
            "alter_id",
            "name",
            "parent",
            "parent_name",
            "alias",
            "opening_balance",
            "gst_in",
            "company",
        ]
        read_only_fields = ["id", "parent"]


class TallyLedgerPayloadSerializer(serializers.Serializer):
    """
    Incoming payload from Tally TCP bridge.
    {
      "LEDGER": [
        {
          "Parent": "Sundry Creditors",
          "Master_Id": "...",
          "Alter_Id": "...",
          "Name": "ABC Pvt Ltd",
          "ALIAS": "ABC",
          "OpeningBalance": "0",
          "GSTIN": "22AAAAA0000A1Z5",
          "Company": "Foo & Co"
        }
      ]
    }
    """
    LEDGER = serializers.ListField(child=serializers.DictField(), allow_empty=False)


# -------- Expense (GET response) --------

class DRCRItemSerializer(serializers.Serializer):
    LEDGERNAME = serializers.CharField()
    AMOUNT = serializers.FloatField()


class ExpenseSyncedEntrySerializer(serializers.Serializer):
    id = serializers.UUIDField()
    voucher = serializers.CharField(allow_blank=True, allow_null=True)
    bill_no = serializers.CharField(allow_blank=True, allow_null=True)
    bill_date = serializers.DateField(allow_null=True)
    total = serializers.FloatField()
    name = serializers.CharField()
    company = serializers.CharField()
    gst_in = serializers.CharField()
    DR_LEDGER = DRCRItemSerializer(many=True)
    CR_LEDGER = DRCRItemSerializer(many=True)
    note = serializers.CharField(allow_blank=True, allow_null=True)
    created_at = serializers.CharField(allow_blank=True, allow_null=True)  # formatted string


class ExpenseSyncedResponseSerializer(serializers.Serializer):
    data = ExpenseSyncedEntrySerializer(many=True)


# -------- Vendor (GET response) --------

class VendorTxnSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    item_name = serializers.CharField(allow_null=True, allow_blank=True)
    item_details = serializers.CharField(allow_null=True, allow_blank=True)
    price = serializers.FloatField(allow_null=True)
    quantity = serializers.IntegerField()
    amount = serializers.FloatField(allow_null=True)
    gst = serializers.CharField(allow_null=True, allow_blank=True)
    igst = serializers.FloatField()
    cgst = serializers.FloatField()
    sgst = serializers.FloatField()


class VendorSyncedEntrySerializer(serializers.Serializer):
    id = serializers.UUIDField()
    bill_no = serializers.CharField(allow_null=True, allow_blank=True)
    bill_date = serializers.DateField(allow_null=True)
    total = serializers.FloatField()
    igst = serializers.FloatField()
    cgst = serializers.FloatField()
    sgst = serializers.FloatField()
    vendor = serializers.DictField()  # { name, company, gst_in }
    customer_id = serializers.UUIDField(allow_null=True)
    transactions = VendorTxnSerializer(many=True)


class VendorSyncedResponseSerializer(serializers.Serializer):
    data = VendorSyncedEntrySerializer(many=True)


# -------- Master (POST from TCP; we just accept anything/JSON) --------

class MasterPayloadSerializer(serializers.Serializer):
    """
    We don't impose a schema yet. Accept raw JSON/text at the view.
    Keep this to let Spectacular produce a schema node.
    """
    payload = serializers.JSONField(required=False)

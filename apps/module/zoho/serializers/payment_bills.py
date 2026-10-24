"""Serializers for Zoho receipt and payment vouchers.

Receipts and payments are the same object read from opposite sides of the
ledger — Zoho's ``customerpayments`` and ``vendorpayments`` differ only in
the party field and the name of the bank/cash account field — so the
analysed-header serializer is written once here and subclassed twice.
"""
from __future__ import annotations

import logging

from rest_framework import serializers

from apps.common.serializers import FileUploadField, UploadedByUserSerializer  # noqa: F401
from apps.module.zoho.models import (
    PaymentBill,
    PaymentZohoBill,
    ReceiptBill,
    ReceiptZohoBill,
    ZohoChartOfAccount,
    ZohoCustomer,
    ZohoVendor,
)
from .bill_base import (
    OrgLookupMixin,
    BaseZohoBillListSerializer,
    BaseZohoBillDetailSerializer,
    BaseZohoBillUploadSerializer,
    BaseZohoBillMultipleUploadSerializer,
)

logger = logging.getLogger(__name__)

_SHARED_FIELDS = [
    "id", "selectBill", "bill_no", "bill_date", "payment_mode", "amount",
    "bank_charges", "tds", "note", "created_at",
]


class _BaseAnalysedPaymentSerializer(OrgLookupMixin, serializers.ModelSerializer):
    """Shared analysed-header serializer. Subclasses name the party field
    and the bank/cash account field, and list them in ``Meta.fields``."""

    #: Names of the FK fields whose querysets must be scoped to the org,
    #: mapped to the model they point at.
    org_scoped_fields: dict = {}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        organization = self._get_organization()
        if not organization:
            return
        for field_name, model in self.org_scoped_fields.items():
            if field_name in self.fields:
                self.fields[field_name].queryset = model.objects.filter(organization=organization)


class ReceiptZohoBillSerializer(_BaseAnalysedPaymentSerializer):
    org_scoped_fields = {
        "customer": ZohoCustomer,
        "deposit_to_account": ZohoChartOfAccount,
    }

    class Meta:
        model = ReceiptZohoBill
        fields = _SHARED_FIELDS + ["customer", "deposit_to_account", "applied_invoices"]
        read_only_fields = ["id", "selectBill", "created_at"]


class PaymentZohoBillSerializer(_BaseAnalysedPaymentSerializer):
    org_scoped_fields = {
        "vendor": ZohoVendor,
        "paid_through_account": ZohoChartOfAccount,
    }

    class Meta:
        model = PaymentZohoBill
        fields = _SHARED_FIELDS + ["vendor", "paid_through_account", "applied_bills"]
        read_only_fields = ["id", "selectBill", "created_at"]


# ---------- Listing / detail / upload ----------

class ZohoReceiptBillSerializer(BaseZohoBillListSerializer):
    class Meta(BaseZohoBillListSerializer.Meta):
        model = ReceiptBill
        fields = BaseZohoBillListSerializer.Meta.fields + ["zoho_payment_id"]
        read_only_fields = BaseZohoBillListSerializer.Meta.read_only_fields + ["zoho_payment_id"]
        ref_name = "ZohoReceiptBill"


class ZohoReceiptBillDetailSerializer(BaseZohoBillDetailSerializer):
    zoho_bill_serializer_class = ReceiptZohoBillSerializer

    class Meta:
        ref_name = "ZohoReceiptBillDetail"


class ZohoReceiptBillUploadSerializer(BaseZohoBillUploadSerializer):
    class Meta(BaseZohoBillUploadSerializer.Meta):
        model = ReceiptBill
        ref_name = "ZohoReceiptBillUploadRequest"


class ZohoReceiptBillMultipleUploadSerializer(BaseZohoBillMultipleUploadSerializer):
    class Meta:
        ref_name = "ZohoReceiptBillMultipleUploadRequest"


class ZohoPaymentBillSerializer(BaseZohoBillListSerializer):
    class Meta(BaseZohoBillListSerializer.Meta):
        model = PaymentBill
        fields = BaseZohoBillListSerializer.Meta.fields + ["zoho_payment_id"]
        read_only_fields = BaseZohoBillListSerializer.Meta.read_only_fields + ["zoho_payment_id"]
        ref_name = "ZohoPaymentBill"


class ZohoPaymentBillDetailSerializer(BaseZohoBillDetailSerializer):
    zoho_bill_serializer_class = PaymentZohoBillSerializer

    class Meta:
        ref_name = "ZohoPaymentBillDetail"


class ZohoPaymentBillUploadSerializer(BaseZohoBillUploadSerializer):
    class Meta(BaseZohoBillUploadSerializer.Meta):
        model = PaymentBill
        ref_name = "ZohoPaymentBillUploadRequest"


class ZohoPaymentBillMultipleUploadSerializer(BaseZohoBillMultipleUploadSerializer):
    class Meta:
        ref_name = "ZohoPaymentBillMultipleUploadRequest"

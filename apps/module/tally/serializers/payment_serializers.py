"""
Serializers for Tally Payment Vouchers.

Structural clone of :mod:`expense_serializers` — same shape so the same
frontend detail component can render either voucher type. See
``apps.module.tally.models.TallyPayment*`` for the underlying models.
"""
import logging

from rest_framework import serializers

from apps.common.serializers import UploadedByUserSerializer  # noqa: F401
from .bill_base import (
    BaseTallyBillSerializer,
    BaseBillUploadSerializer,
    BillAnalysisRequestSerializer as _BillAnalysisRequestSerializer,  # noqa: F401
    BillSyncRequestSerializer,  # noqa: F401 - re-export
    BaseBillDetailSerializer,
)
from ..models import (
    TallyPaymentBill,
    TallyPaymentAnalyzedBill,
    TallyPaymentAnalyzedProduct,
)

logger = logging.getLogger(__name__)


PaymentBillAnalysisRequestSerializer = _BillAnalysisRequestSerializer


class TallyPaymentBillSerializer(BaseTallyBillSerializer):
    """Tally payment voucher list serializer."""

    class Meta:
        model = TallyPaymentBill
        fields = BaseTallyBillSerializer.base_fields
        read_only_fields = BaseTallyBillSerializer.base_read_only_fields


class TallyPaymentAnalyzedProductSerializer(serializers.ModelSerializer):
    chart_of_accounts_name = serializers.CharField(source="chart_of_accounts.name", read_only=True)

    class Meta:
        model = TallyPaymentAnalyzedProduct
        fields = [
            "id", "item_details", "chart_of_accounts", "chart_of_accounts_name",
            "amount", "debit_or_credit", "created_at",
        ]
        read_only_fields = ["id", "created_at", "chart_of_accounts_name"]


class TallyPaymentAnalyzedBillSerializer(serializers.ModelSerializer):
    products = TallyPaymentAnalyzedProductSerializer(many=True, read_only=True)
    # ``vendor`` here holds the Bank/Cash ledger for a payment voucher —
    # kept named ``vendor_name`` so the shared frontend detail component
    # doesn't need voucher-type-specific field switching.
    vendor_name = serializers.CharField(source="vendor.name", read_only=True)
    selected_bill_name = serializers.CharField(source="selected_bill.bill_munshi_name", read_only=True)

    def get_consolidate_prod_data(self, obj):
        """Return consolidated line items if any exist."""
        try:
            if not hasattr(obj, "consolidated_products"):
                return []
            consolidated_products = obj.consolidated_products.all()
            return [{
                "id": str(cp.id),
                "item_details": cp.item_details,
                "chart_of_accounts": (
                    str(cp.chart_of_accounts.name) if cp.chart_of_accounts else ""
                ),
                "chart_of_accounts_id": (
                    str(cp.chart_of_accounts_id) if cp.chart_of_accounts_id else None
                ),
                "amount": float(cp.amount or 0),
                "debit_or_credit": cp.debit_or_credit or "debit",
                "original_entries_count": cp.original_entries_count or 0,
                "consolidation_notes": cp.consolidation_notes or "",
                "created_at": cp.created_at.isoformat() if cp.created_at else None,
            } for cp in consolidated_products]
        except Exception as e:
            logger.error(
                "Error getting consolidated payment products for %s: %s",
                type(obj), e,
            )
            return []

    def get_gst_lines_data(self, obj):
        """Serialise multi-rate GST lines for the FE detail page."""
        try:
            return [
                {
                    "id": str(line.id),
                    "rate": line.rate or "",
                    "tax_type": line.tax_type,
                    "amount": float(line.amount or 0),
                    "ledger_id": str(line.ledger_id) if line.ledger_id else None,
                    "ledger": str(line.ledger_id) if line.ledger_id else None,
                    "ledger_name": line.ledger.name if line.ledger else "",
                    "debit_or_credit": line.debit_or_credit or "debit",
                }
                for line in obj.gst_lines.all()
            ]
        except Exception as e:
            logger.error("Error serialising payment gst_lines for %s: %s", type(obj), e)
            return []

    def to_representation(self, instance):
        try:
            data = super().to_representation(instance)
            data["consolidate_prod"] = self.get_consolidate_prod_data(instance)
            data["gst_lines"] = self.get_gst_lines_data(instance)
            return data
        except Exception as e:
            logger.error(
                "Error in TallyPaymentAnalyzedBillSerializer.to_representation for %s: %s",
                type(instance), e,
            )
            data = super().to_representation(instance)
            data["consolidate_prod"] = []
            data["gst_lines"] = []
            return data

    class Meta:
        model = TallyPaymentAnalyzedBill
        fields = [
            "id", "selected_bill", "selected_bill_name", "vendor", "vendor_name",
            "voucher", "bill_no", "bill_date", "due_date", "total",
            "vendor_amount", "vendor_debit_or_credit",
            "igst", "igst_taxes", "igst_debit_or_credit",
            "cgst", "cgst_taxes", "cgst_debit_or_credit",
            "sgst", "sgst_taxes", "sgst_debit_or_credit",
            "tds", "tds_taxes", "tds_debit_or_credit",
            "other_adjustment", "other_adjustment_taxes", "other_adjustment_debit_or_credit",
            # round_off was missing → save persisted but reload dropped it.
            "round_off", "round_off_taxes", "round_off_debit_or_credit",
            "note", "consolidate", "products", "created_at",
        ]
        read_only_fields = ["id", "created_at", "vendor_name", "selected_bill_name", "products"]


class PaymentBillUploadSerializer(BaseBillUploadSerializer):
    """Serializer for payment voucher file upload."""
    bill_model = TallyPaymentBill

    file_type = serializers.ChoiceField(
        choices=TallyPaymentBill.BillType.choices,
        default=TallyPaymentBill.BillType.SINGLE,
        help_text="Type: SINGLE (each file is separate voucher) or MULTI (PDF pages split)",
    )


class PaymentBillVerificationSerializer(serializers.Serializer):
    """Payment voucher verification payload (mirrors expense)."""
    vendor_id = serializers.UUIDField(required=False, allow_null=True)
    voucher = serializers.CharField(max_length=255, required=False)
    bill_no = serializers.CharField(max_length=50, required=False)
    bill_date = serializers.DateField(required=False, allow_null=True)
    note = serializers.CharField(max_length=500, required=False)
    igst = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, default=0)
    cgst = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, default=0)
    sgst = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, default=0)
    tds = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, default=0)
    other_adjustment = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, default=0)
    igst_taxes_id = serializers.UUIDField(required=False, allow_null=True)
    cgst_taxes_id = serializers.UUIDField(required=False, allow_null=True)
    sgst_taxes_id = serializers.UUIDField(required=False, allow_null=True)
    tds_taxes_id = serializers.UUIDField(required=False, allow_null=True)
    other_adjustment_taxes_id = serializers.UUIDField(required=False, allow_null=True)

    products = serializers.ListField(
        child=serializers.DictField(), required=False,
        help_text="List of product updates",
    )

    def validate_products(self, value):
        for product in value:
            if "id" not in product:
                raise serializers.ValidationError("Product ID is required for each product")
        return value


class PaymentBillSyncResponseSerializer(serializers.Serializer):
    id = serializers.CharField()
    voucher = serializers.CharField()
    bill_no = serializers.CharField()
    bill_date = serializers.CharField(allow_null=True)
    total = serializers.FloatField()
    name = serializers.CharField()
    company = serializers.CharField()
    gst_in = serializers.CharField()
    DR_LEDGER = serializers.ListField(child=serializers.DictField())
    CR_LEDGER = serializers.ListField(child=serializers.DictField())
    note = serializers.CharField()


class TallyPaymentBillDetailSerializer(BaseBillDetailSerializer):
    """Enhanced Tally payment voucher serializer with analysed data."""

    analyzed_bill_model = TallyPaymentAnalyzedBill

    def get_analyzed_bill(self, obj):
        try:
            analyzed_bill = TallyPaymentAnalyzedBill.objects.filter(selected_bill=obj).first()
            if analyzed_bill:
                return TallyPaymentAnalyzedBillSerializer(analyzed_bill).data
        except Exception as e:
            logger.error(f"Error getting analyzed payment voucher for {obj.id}: {e}")
        return None

    class Meta:
        model = TallyPaymentBill
        fields = BaseBillDetailSerializer.base_fields
        read_only_fields = BaseBillDetailSerializer.base_read_only_fields

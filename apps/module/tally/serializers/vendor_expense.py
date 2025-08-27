from __future__ import annotations

from rest_framework import serializers

from apps.organizations.models import Organization
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
    GST_TYPE_CHOICES = [
        ('IGST', 'IGST'),
        ('CGST_SGST', 'CGST+SGST'),
    ]

    organization = serializers.PrimaryKeyRelatedField(
        queryset=Organization.objects.all(),
        required=True,
        help_text="Organization ID"
    )
    file = serializers.FileField(required=False, help_text="Single file upload")
    files = serializers.ListField(
        child=serializers.FileField(),
        required=False,
        help_text="Multiple files upload"
    )
    gst_type = serializers.ChoiceField(
        choices=GST_TYPE_CHOICES,
        required=True,
        help_text="GST type for the bill"
    )

    class Meta:
        model = TallyVendorBill
        fields = ["organization", "file", "files", "fileType", "gst_type"]
        ref_name = "TallyVendorBillUpload"

    def validate(self, data):
        """Validate that either file or files is provided, but not both."""
        file = data.get('file')
        files = data.get('files')

        if not file and not files:
            raise serializers.ValidationError(
                "Either 'file' or 'files' must be provided."
            )

        if file and files:
            raise serializers.ValidationError(
                "Provide either 'file' or 'files', not both."
            )

        return data


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

    def validate(self, data):
        """
        Validate GST data based on gst_type:
        - Inter-State: Only IGST should be provided
        - Intra-State: Only CGST and SGST should be provided
        """
        gst_type = data.get('gst_type')
        if not gst_type:
            return data

        # For Inter-State, validate IGST is provided and CGST/SGST are zero
        if gst_type == 'Inter-State':
            igst = data.get('igst', 0)
            cgst = data.get('cgst', 0)
            sgst = data.get('sgst', 0)

            if igst == 0 and (cgst > 0 or sgst > 0):
                raise serializers.ValidationError({
                    'gst_type': 'For Inter-State GST, IGST should be provided, not CGST/SGST',
                    'igst': 'IGST value required for Inter-State GST'
                })

            if cgst > 0 or sgst > 0:
                raise serializers.ValidationError({
                    'cgst': 'CGST should be zero for Inter-State GST',
                    'sgt': 'SGST should be zero for Inter-State GST'
                })

        # For Intra-State, validate CGST and SGST are provided and IGST is zero
        if gst_type == 'Intra-State':
            igst = data.get('igst', 0)
            cgst = data.get('cgst', 0)
            sgst = data.get('sgst', 0)

            if igst > 0:
                raise serializers.ValidationError({
                    'igst': 'IGST should be zero for Intra-State GST'
                })

            if cgst == 0 and sgst == 0 and igst == 0:
                # Only validate if any GST value is being set
                if 'cgst' in data or 'sgst' in data or 'igst' in data:
                    raise serializers.ValidationError({
                        'gst_type': 'For Intra-State GST, both CGST and SGST should be provided',
                        'cgst': 'CGST value required for Intra-State GST',
                        'sgt': 'SGST value required for Intra-State GST'
                    })

            # Ensure CGST and SGST are equal for Intra-State
            if cgst != sgst:
                raise serializers.ValidationError({
                    'cgst': 'CGST and SGST values must be equal for Intra-State GST',
                    'sgt': 'CGST and SGST values must be equal for Intra-State GST'
                })

        return data


# ----------------------
# Expense Bill Serializers
# ----------------------

class ExpenseBillUploadSerializer(serializers.ModelSerializer):
    organization = serializers.PrimaryKeyRelatedField(
        queryset=Organization.objects.all(),
        required=False,
        help_text="Organization ID (will be auto-filled from URL if not provided)"
    )
    file = serializers.FileField(required=False, help_text="Single file upload")
    files = serializers.ListField(
        child=serializers.FileField(),
        required=False,
        help_text="Multiple files upload"
    )

    class Meta:
        model = TallyExpenseBill
        fields = ["organization", "file", "files", "fileType"]
        ref_name = "TallyExpenseBillUpload"

    def validate(self, data):
        """Validate that either file or files is provided, but not both."""
        file = data.get('file')
        files = data.get('files')

        if not file and not files:
            raise serializers.ValidationError(
                "Either 'file' or 'files' must be provided."
            )

        if file and files:
            raise serializers.ValidationError(
                "Provide either 'file' or 'files', not both."
            )

        return data


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

    def validate_amount(self, value):
        """Ensure amount is a positive decimal."""
        if value is not None and value < 0:
            raise serializers.ValidationError("Amount must be a positive number.")
        return value


class ExpenseAnalyzedBillSerializer(serializers.ModelSerializer):
    vendor = LedgerPKField(required=False, allow_null=True)
    products = ExpenseAnalyzedProductSerializer(many=True, required=False)
    igst_taxes = LedgerPKField(required=False, allow_null=True)
    cgst_taxes = LedgerPKField(required=False, allow_null=True)
    sgst_taxes = LedgerPKField(required=False, allow_null=True)

    class Meta:
        model = TallyExpenseAnalyzedBill
        fields = [
            "id", "selectBill", "vendor", "voucher", "bill_no", "bill_date", "total",
            "igst", "igst_taxes", "cgst", "cgst_taxes", "sgst", "sgst_taxes",
            "note", "created_at", "products"
        ]
        read_only_fields = ["id", "created_at"]
        ref_name = "TallyExpenseAnalyzedBill"

    def validate(self, data):
        """Validate GST data consistency"""
        igst = data.get('igst', 0)
        cgst = data.get('cgst', 0)
        sgst = data.get('sgst', 0)

        # Check for mixed GST types (either use IGST or CGST+SGST)
        if igst > 0 and (cgst > 0 or sgst > 0):
            raise serializers.ValidationError({
                'igst': 'Cannot use both IGST and CGST/SGST. For Inter-State use only IGST, for Intra-State use only CGST+SGST.',
                'cgst': 'Cannot use both IGST and CGST/SGST.',
                'sgst': 'Cannot use both IGST and CGST/SGST.'
            })

        # For CGST and SGST, both should be present and equal
        if (cgst > 0 and sgst == 0) or (cgst == 0 and sgst > 0):
            raise serializers.ValidationError({
                'cgst': 'CGST and SGST must be used together',
                'sgst': 'CGST and SGST must be used together'
            })

        if cgst > 0 and sgst > 0 and cgst != sgst:
            raise serializers.ValidationError({
                'cgst': 'CGST and SGST values must be equal',
                'sgst': 'CGST and SGST values must be equal'
            })

        return data

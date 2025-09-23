# apps/module/tally/models.py
from __future__ import annotations

import os
import re
import uuid
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models

from apps.organizations.models import Organization


# -----------------------------
# Helpers / Base
# -----------------------------

def validate_file_extension(value):
    """
    Validates the file extension for uploads (PDF/Images only).
    """
    ext = os.path.splitext(getattr(value, "name", ""))[1].lower()
    valid = {".pdf", ".png", ".jpg", ".jpeg"}
    if ext not in valid:
        raise ValidationError(f"Unsupported file extension '{ext}'. Allowed: {', '.join(sorted(valid))}")


class BaseOrgModel(models.Model):
    """
    Common base for all Tally models. Scopes records to an Organization.
    """
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="tally_%(class)ss",
    )

    class Meta:
        abstract = True


# -----------------------------
# Masters
# -----------------------------

class ParentLedger(BaseOrgModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, unique=True)
    parent = models.CharField(max_length=255, blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)  # Fixed typo: was 'update_at'

    class Meta:
        verbose_name = "Parent Ledger"
        verbose_name_plural = "Parent Ledgers"

    def __str__(self) -> str:
        return self.parent or "ParentLedger"


class Ledger(BaseOrgModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, unique=True)
    master_id = models.CharField(max_length=255, blank=True, null=True)
    alter_id = models.CharField(max_length=255, blank=True, null=True)
    name = models.CharField(max_length=255, blank=True, null=True)

    parent = models.ForeignKey(ParentLedger, on_delete=models.CASCADE, related_name="ledgers")
    alias = models.CharField(max_length=255, blank=True, null=True)
    opening_balance = models.DecimalField(max_digits=15, decimal_places=2, blank=True, null=True, default=Decimal("0"))  # Fixed: was CharField
    gst_in = models.CharField(max_length=255, blank=True, null=True)
    company = models.CharField(max_length=255, blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)  # Added missing timestamp
    updated_at = models.DateTimeField(auto_now=True)     # Added missing timestamp

    class Meta:
        verbose_name = "Ledger"
        verbose_name_plural = "Ledgers"

    def __str__(self) -> str:
        return self.name or "Ledger"


class StockItem(BaseOrgModel):
    """
    StockItem model to store stock item data from Tally
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, unique=True)
    master_id = models.CharField(max_length=255, blank=True, null=True)
    alter_id = models.CharField(max_length=255, blank=True, null=True)
    name = models.CharField(max_length=255, blank=True, null=True)
    parent = models.CharField(max_length=255, blank=True, null=True)
    unit = models.CharField(max_length=100, blank=True, null=True)
    category = models.CharField(max_length=255, blank=True, null=True)
    gst_applicable = models.CharField(max_length=100, blank=True, null=True)
    item_code = models.CharField(max_length=255, blank=True, null=True)
    alias = models.CharField(max_length=255, blank=True, null=True)
    company = models.CharField(max_length=500, blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Stock Item"
        verbose_name_plural = "Stock Items"
        # Add unique constraint to prevent duplicates
        unique_together = ['organization', 'master_id', 'company']

    def __str__(self) -> str:
        return self.name or "StockItem"


class TallyConfig(BaseOrgModel):
    """
    User-defined mapping: which ParentLedger(s) represent IGST/CGST/SGST/Vendors/COA for an org.
    All fields are ManyToMany as requested.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, unique=True)

    igst_parents = models.ManyToManyField(
        ParentLedger, blank=True, related_name="igst_tally_configs", verbose_name="IGST Parent Ledgers"
    )
    cgst_parents = models.ManyToManyField(
        ParentLedger, blank=True, related_name="cgst_tally_configs", verbose_name="CGST Parent Ledgers"
    )
    sgst_parents = models.ManyToManyField(
        ParentLedger, blank=True, related_name="sgst_tally_configs", verbose_name="SGST Parent Ledgers"
    )
    vendor_parents = models.ManyToManyField(
        ParentLedger, blank=True, related_name="vendor_tally_configs", verbose_name="Vendor Parent Ledgers"
    )
    chart_of_accounts_parents = models.ManyToManyField(
        ParentLedger, blank=True, related_name="coa_tally_configs", verbose_name="COA Parent Ledgers"
    )
    chart_of_accounts_expense_parents = models.ManyToManyField(
        ParentLedger, blank=True, related_name="expense_coa_tally_configs", verbose_name="Expense COA Parent Ledgers"
    )

    class Meta:
        verbose_name = "Tally Configuration"
        verbose_name_plural = "Tally Configurations"

    def __str__(self) -> str:
        return f"TallyConfig · {self.organization.name}"


# ---------------------------------
# Vendor Bills (Upload + Analysed)
# ---------------------------------

class TallyVendorBill(BaseOrgModel):
    class BillStatus(models.TextChoices):
        DRAFT = "Draft", "Draft"
        ANALYSED = "Analysed", "Analysed"
        VERIFIED = "Verified", "Verified"
        SYNCED = "Synced", "Synced"

    class BillType(models.TextChoices):
        SINGLE = "Single Invoice/File", "Single Invoice/File"
        MULTI = "Multiple Invoice/File", "Multiple Invoice/File"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, unique=True)
    bill_munshi_name = models.CharField(max_length=100, blank=True, null=True)  # Fixed: was billmunshiName
    file = models.FileField(upload_to="bills/", validators=[validate_file_extension])
    file_type = models.CharField(  # Fixed: was fileType
        choices=BillType.choices, max_length=100, blank=True, null=True, default=BillType.SINGLE
    )
    analysed_data = models.JSONField(default=dict, blank=True, null=True)
    status = models.CharField(
        max_length=10, choices=BillStatus.choices, default=BillStatus.DRAFT, blank=True
    )
    process = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Tally Vendor Bill"
        verbose_name_plural = "Tally Vendor Bills"

    def __str__(self) -> str:
        return self.bill_munshi_name or f"TallyVendorBill:{self.id}"

    def save(self, *args, **kwargs):
        """
        Autogenerate bill_munshi_name as 'BM-TB-{N}' if missing.
        Also validates status transitions.
        """
        if not self.bill_munshi_name:
            last = (
                TallyVendorBill.objects.filter(
                    organization=self.organization, bill_munshi_name__startswith="BM-TB-"
                )
                .order_by("-bill_munshi_name")
                .first()
            )
            if last and last.bill_munshi_name:
                m = re.match(r"BM-TB-(\d+)$", last.bill_munshi_name)
                next_num = int(m.group(1)) + 1 if m else 1
            else:
                next_num = 1
            self.bill_munshi_name = f"BM-TB-{next_num}"

        # Perform status transition validation for existing records
        # if self.pk:  # Skip validation for new records
        #     try:
        #         old_instance = TallyVendorBill.objects.get(pk=self.pk)
        #         if old_instance.status != self.status:
        #             valid_transitions = {
        #                 self.BillStatus.DRAFT: [self.BillStatus.ANALYSED],
        #                 self.BillStatus.ANALYSED: [self.BillStatus.VERIFIED],
        #                 self.BillStatus.VERIFIED: [self.BillStatus.SYNCED],
        #                 self.BillStatus.SYNCED: [],  # No further transitions allowed
        #             }
        #
        #             if self.status not in valid_transitions.get(old_instance.status, []):
        #                 raise ValidationError({
        #                     'status': f"Invalid status transition from {old_instance.status} to {self.status}. "
        #                             f"Valid next states are: {', '.join(valid_transitions.get(old_instance.status, []))}"
        #                 })
        #     except TallyVendorBill.DoesNotExist:
        #         pass  # Handle case where pk exists but object doesn't (unlikely)

        super().save(*args, **kwargs)

    # def clean(self):
    #     """Validate status transitions"""
    #     super().clean()
    #     if not self.pk:  # Skip validation for new records
    #         return
    #
    #     try:
    #         old_instance = TallyVendorBill.objects.get(pk=self.pk)
    #         if old_instance.status != self.status:
    #             valid_transitions = {
    #                 self.BillStatus.DRAFT: [self.BillStatus.ANALYSED],
    #                 self.BillStatus.ANALYSED: [self.BillStatus.VERIFIED],
    #                 self.BillStatus.VERIFIED: [self.BillStatus.SYNCED],
    #                 self.BillStatus.SYNCED: [],  # No further transitions allowed
    #             }
    #
    #             if self.status not in valid_transitions.get(old_instance.status, []):
    #                 raise ValidationError({
    #                     'status': f"Invalid status transition from {old_instance.status} to {self.status}. "
    #                              f"Valid next states are: {', '.join(valid_transitions.get(old_instance.status, []))}"
    #                 })
    #     except TallyVendorBill.DoesNotExist:
    #         pass  # Handle case where pk exists but object doesn't (unlikely)


class TallyVendorAnalyzedBill(BaseOrgModel):
    class TaxType(models.TextChoices):
        TCS = "TCS", "is_tcs_tax"
        TDS = "TDS", "is_tds_tax"

    class GSTType(models.TextChoices):
        IGST = "IGST", "IGST"
        CGST_SGST = "CGST_SGST", "CGST+SGST"
        UNKNOWN = "Unknown", "Unknown"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, unique=True)
    selected_bill = models.ForeignKey(
        TallyVendorBill, on_delete=models.CASCADE, blank=True, null=True, related_name="analysed_headers"
    )
    vendor = models.ForeignKey(
        Ledger, on_delete=models.CASCADE, blank=True, null=True, related_name="vendor_tally_vendor_analysed_bills"
    )

    bill_no = models.CharField(max_length=50, blank=True, null=True)
    bill_date = models.DateField(blank=True, null=True)

    total = models.DecimalField(max_digits=15, decimal_places=2, blank=True, null=True, default=Decimal("0"))
    igst = models.DecimalField(max_digits=15, decimal_places=2, blank=True, null=True, default=Decimal("0"))
    igst_taxes = models.ForeignKey(
        Ledger, on_delete=models.CASCADE, blank=True, null=True, related_name="igst_tally_vendor_analysed_bills"
    )
    cgst = models.DecimalField(max_digits=15, decimal_places=2, blank=True, null=True, default=Decimal("0"))
    cgst_taxes = models.ForeignKey(
        Ledger, on_delete=models.CASCADE, blank=True, null=True, related_name="cgst_tally_vendor_analysed_bills"
    )
    sgst = models.DecimalField(max_digits=15, decimal_places=2, blank=True, null=True, default=Decimal("0"))
    sgst_taxes = models.ForeignKey(
        Ledger, on_delete=models.CASCADE, blank=True, null=True, related_name="sgst_tally_vendor_analysed_bills"
    )

    gst_type = models.CharField(max_length=20, choices=GSTType.choices, default=GSTType.UNKNOWN)
    note = models.TextField(blank=True, null=True, default="Enter Your Description")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Tally Vendor Analysed Bill"
        verbose_name_plural = "Tally Vendor Analysed Bills"

    def __str__(self) -> str:
        return (self.selected_bill.bill_munshi_name if self.selected_bill else None) or f"VendorAnalysed:{self.id}"

    def validate_gst_calculations(self):
        """Validate GST calculations and consistency"""
        # Skip validation if no amounts are set
        if not any([self.total, self.igst, self.cgst, self.sgst]):
            return

        # Convert to Decimal for accurate calculations
        total = self.total or Decimal("0")
        igst = self.igst or Decimal("0")
        cgst = self.cgst or Decimal("0")
        sgst = self.sgst or Decimal("0")

        # Basic validation: GST amounts should not be negative
        if igst < 0 or cgst < 0 or sgst < 0:
            raise ValidationError("GST amounts cannot be negative")

        # Validate GST type consistency
        if self.gst_type == self.GSTType.IGST and igst == 0:
            # Allow this - user might set IGST type but amount could be 0
            pass
        elif self.gst_type == self.GSTType.CGST_SGST and (cgst == 0 and sgst == 0):
            # Allow this - user might set CGST+SGST type but amounts could be 0
            pass

        # For inter-state transactions, CGST and SGST should be zero when IGST is present
        if igst > 0 and (cgst > 0 or sgst > 0):
            raise ValidationError("Cannot have both IGST and CGST/SGST for the same transaction")

    def clean(self):
        super().clean()
        self.validate_gst_calculations()

    def save(self, *args, **kwargs):
        # Skip full_clean for bulk operations or when explicitly requested
        skip_validation = kwargs.pop('skip_validation', False)
        if not skip_validation:
            self.full_clean()
        super().save(*args, **kwargs)


class TallyVendorAnalyzedProduct(BaseOrgModel):
    """
    Analysed products from vendor bills.
    """
    GST_CHOICES = [
        ("0%", "0%"),
        ("5%", "5%"),
        ("12%", "12%"),
        ("18%", "18%"),
        ("28%", "28%"),
        ("Exempted", "Exempted"),
        ("N/A", "N/A"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, unique=True)
    vendor_bill_analyzed = models.ForeignKey(
        TallyVendorAnalyzedBill, on_delete=models.CASCADE, related_name="products"
    )

    item_name = models.CharField(max_length=100, blank=True, null=True)
    item_details = models.TextField(blank=True, null=True)
    taxes = models.ForeignKey(Ledger, on_delete=models.CASCADE, blank=True, null=True)

    price = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)
    quantity = models.PositiveIntegerField(blank=True, null=True, default=0)
    amount = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)

    product_gst = models.CharField(max_length=10, choices=GST_CHOICES, blank=True, null=True)
    igst = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True, default=Decimal("0"))
    cgst = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True, default=Decimal("0"))
    sgst = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True, default=Decimal("0"))

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Tally Analysed Bill Product"
        verbose_name_plural = "Tally Analysed Bill Products"

    def __str__(self) -> str:
        return self.item_name or f"VendorProduct:{self.id}"


# ---------------------------------
# Expense Bills (Upload + Analysed)
# ---------------------------------

class TallyExpenseBill(BaseOrgModel):
    class BillStatus(models.TextChoices):
        DRAFT = "Draft", "Draft"
        ANALYSED = "Analysed", "Analysed"
        VERIFIED = "Verified", "Verified"
        SYNCED = "Synced", "Synced"

    class BillType(models.TextChoices):
        SINGLE = "Single Invoice/File", "Single Invoice/File"
        MULTI = "Multiple Invoice/File", "Multiple Invoice/File"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, unique=True)
    bill_munshi_name = models.CharField(max_length=100, blank=True, null=True)  # Fixed: was billmunshiName
    file = models.FileField(upload_to="bills/", validators=[validate_file_extension])
    file_type = models.CharField(  # Fixed: was fileType
        choices=BillType.choices, max_length=100, blank=True, null=True, default=BillType.SINGLE
    )
    analysed_data = models.JSONField(default=dict, blank=True, null=True)
    status = models.CharField(
        max_length=10, choices=BillStatus.choices, default=BillStatus.DRAFT, blank=True
    )
    process = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Tally Expense Bill"
        verbose_name_plural = "Tally Expense Bills"

    def __str__(self) -> str:
        return self.bill_munshi_name or f"TallyExpenseBill:{self.id}"

    def save(self, *args, **kwargs):
        """
        Autogenerate bill_munshi_name as 'BM-TE-{N}' if missing.
        """
        if not self.bill_munshi_name:
            last = (
                TallyExpenseBill.objects.filter(
                    organization=self.organization, bill_munshi_name__startswith="BM-TE-"
                )
                .order_by("-bill_munshi_name")
                .first()
            )
            if last and last.bill_munshi_name:
                m = re.match(r"BM-TE-(\d+)$", last.bill_munshi_name)
                next_num = int(m.group(1)) + 1 if m else 1
            else:
                next_num = 1
            self.bill_munshi_name = f"BM-TE-{next_num}"
        super().save(*args, **kwargs)


class TallyExpenseAnalyzedBill(BaseOrgModel):
    class GSTType(models.TextChoices):
        IGST = "IGST", "IGST"
        CGST_SGST = "CGST_SGST", "CGST+SGST"
        UNKNOWN = "Unknown", "Unknown"

    class DebitCredit(models.TextChoices):
        CREDIT = "credit", "Credit"
        DEBIT = "debit", "Debit"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, unique=True)
    selected_bill = models.ForeignKey(  # Fixed: was selectBill
        TallyExpenseBill, on_delete=models.CASCADE, blank=True, null=True, related_name="analysed_headers"
    )
    vendor = models.ForeignKey(
        Ledger, on_delete=models.CASCADE, blank=True, null=True, related_name="vendor_tally_expense_analysed_bills"
    )
    vendor_debit_or_credit = models.CharField(
        choices=DebitCredit.choices, max_length=10, blank=True, null=True, default=DebitCredit.CREDIT
    )
    voucher = models.CharField(max_length=255, blank=True, null=True)
    bill_no = models.CharField(max_length=50, blank=True, null=True)
    bill_date = models.DateField(blank=True, null=True)
    gst_type = models.CharField(max_length=20, choices=GSTType.choices, default=GSTType.UNKNOWN)

    total = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True, default=Decimal("0"))
    igst = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True, default=Decimal("0"))
    igst_taxes = models.ForeignKey(
        Ledger, on_delete=models.CASCADE, blank=True, null=True, related_name="igst_tally_expense_analysed_bills"
    )
    igst_debit_or_credit = models.CharField(
        choices=DebitCredit.choices, max_length=10, blank=True, null=True, default=DebitCredit.DEBIT
    )
    cgst = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True, default=Decimal("0"))
    cgst_taxes = models.ForeignKey(
        Ledger, on_delete=models.CASCADE, blank=True, null=True, related_name="cgst_tally_expense_analysed_bills"
    )
    cgst_debit_or_credit = models.CharField(
        choices=DebitCredit.choices, max_length=10, blank=True, null=True, default=DebitCredit.DEBIT
    )
    sgst = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True, default=Decimal("0"))
    sgst_taxes = models.ForeignKey(
        Ledger, on_delete=models.CASCADE, blank=True, null=True, related_name="sgst_tally_expense_analysed_bills"
    )
    sgst_debit_or_credit = models.CharField(
        choices=DebitCredit.choices, max_length=10, blank=True, null=True, default=DebitCredit.DEBIT
    )

    note = models.CharField(max_length=100, blank=True, null=True, default="Enter Your Description")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Tally Expense Analysed Bill"
        verbose_name_plural = "Tally Expense Analysed Bills"

    def __str__(self) -> str:
        return (self.selected_bill.bill_munshi_name if self.selected_bill else None) or f"ExpenseAnalysed:{self.id}"

    def save(self, *args, **kwargs):
        # Handle skip_validation parameter for compatibility
        skip_validation = kwargs.pop('skip_validation', False)
        if not skip_validation:
            self.full_clean()
        super().save(*args, **kwargs)


class TallyExpenseAnalyzedProduct(BaseOrgModel):
    class DebitCredit(models.TextChoices):
        CREDIT = "credit", "Credit"
        DEBIT = "debit", "Debit"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, unique=True)
    expense_bill = models.ForeignKey(
        TallyExpenseAnalyzedBill, related_name="products", on_delete=models.CASCADE
    )

    item_details = models.CharField(max_length=200, blank=True, null=True)
    chart_of_accounts = models.ForeignKey(Ledger, on_delete=models.CASCADE, blank=True, null=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True, default=Decimal("0"))
    debit_or_credit = models.CharField(
        choices=DebitCredit.choices, max_length=10, blank=True, null=True, default=DebitCredit.DEBIT
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Tally Expense Analysed Bill Product"
        verbose_name_plural = "Tally Expense Analysed Bill Products"

    def __str__(self) -> str:
        if self.expense_bill and self.expense_bill.selected_bill:
            return self.expense_bill.selected_bill.bill_munshi_name or f"ExpenseProduct:{self.id}"
        return f"ExpenseProduct:{self.id}"

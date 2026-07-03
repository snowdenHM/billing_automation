# apps/module/tally/models.py
from __future__ import annotations

import re
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models

from apps.organizations.models import Organization
from apps.common.validators import (
    bill_upload_path,
    validate_bill_file_size,
    validate_file_extension,
)


# -----------------------------
# Helpers / Base
# -----------------------------


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


def _pick_round_off_ledger(organization):
    """Return a Ledger to use as the Round Off ledger for the given org.

    Strategy:
      1. Use any ledger whose parent is in `TallyConfig.round_off_parents`.
      2. If TallyConfig has none configured, fall back to a ledger whose name
         contains 'round' (case-insensitive) under any Indirect Expenses parent.
      3. Otherwise return None — the caller leaves round_off_taxes blank.
    """
    if organization is None:
        return None
    try:
        config = TallyConfig.objects.filter(organization=organization).first()
        if config:
            parents = config.round_off_parents.all()
            if parents.exists():
                ledger = (
                    Ledger.objects
                    .filter(organization=organization, parent__in=parents)
                    .order_by('name')
                    .first()
                )
                if ledger:
                    return ledger
        return (
            Ledger.objects
            .filter(organization=organization, name__icontains='round')
            .order_by('name')
            .first()
        )
    except Exception:
        return None


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
    opening_balance = models.DecimalField(max_digits=15, decimal_places=2, blank=True, null=True,
                                          default=Decimal("0"))  # Fixed: was CharField
    gst_in = models.CharField(max_length=255, blank=True, null=True)
    company = models.CharField(max_length=255, blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)  # Added missing timestamp
    updated_at = models.DateTimeField(auto_now=True)  # Added missing timestamp

    class Meta:
        verbose_name = "Ledger"
        verbose_name_plural = "Ledgers"
        # Ensure master_id is unique per organization
        constraints = [
            models.UniqueConstraint(
                fields=['master_id', 'organization'],
                name='unique_master_id_per_organization',
                condition=models.Q(master_id__isnull=False) & ~models.Q(master_id='')
            )
        ]

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
    tally_product_allow_sync = models.BooleanField(default=False)
    igst_parents = models.ManyToManyField(
        'ParentLedger',
        blank=True,
        related_name="igst_tally_configs",
        verbose_name="IGST Parent Ledgers",
        db_table='tally_config_igst_parents'
    )
    cgst_parents = models.ManyToManyField(
        'ParentLedger',
        blank=True,
        related_name="cgst_tally_configs",
        verbose_name="CGST Parent Ledgers",
        db_table='tally_config_cgst_parents'
    )
    sgst_parents = models.ManyToManyField(
        'ParentLedger',
        blank=True,
        related_name="sgst_tally_configs",
        verbose_name="SGST Parent Ledgers",
        db_table='tally_config_sgst_parents'
    )
    vendor_parents = models.ManyToManyField(
        'ParentLedger',
        blank=True,
        related_name="vendor_tally_configs",
        verbose_name="Vendor Parent Ledgers",
        db_table='tally_config_vendor_parents'
    )
    chart_of_accounts_parents = models.ManyToManyField(
        'ParentLedger',
        blank=True,
        related_name="coa_tally_configs",
        verbose_name="COA Parent Ledgers",
        db_table='tally_config_coa_parents'
    )
    chart_of_accounts_expense_parents = models.ManyToManyField(
        'ParentLedger',
        blank=True,
        related_name="expense_coa_tally_configs",
        verbose_name="Expense COA Parent Ledgers",
        db_table='tally_config_expense_coa_parents'
    )
    tds_parents = models.ManyToManyField(
        'ParentLedger',
        blank=True,
        related_name="tds_tally_configs",
        verbose_name="TDS Parent Ledgers",
        db_table='tally_config_tds_parents'
    )
    payment_parents = models.ManyToManyField(
        'ParentLedger',
        blank=True,
        related_name="payment_tally_configs",
        verbose_name="Payment Parent Ledgers",
        db_table='tally_config_payment_parents'
    )
    round_off_parents = models.ManyToManyField(
        'ParentLedger',
        blank=True,
        related_name="round_off_tally_configs",
        verbose_name="Round Off Parent Ledgers",
        help_text="Parent ledgers (typically 'Indirect Expenses') used to source the Round Off ledger.",
        db_table='tally_config_round_off_parents'
    )

    class Meta:
        verbose_name = "Tally Configuration"
        verbose_name_plural = "Tally Configurations"

    def __str__(self) -> str:
        return f"TallyConfig · {self.organization.name}"


class GstRateLedgerMapping(BaseOrgModel):
    """
    Per-organization mapping of GST rate → CGST / SGST / IGST ledgers.

    Used for line-item level tax assignment on vendor bills with mixed GST rates.
    One row per (organization, rate). Standard rates: 0, 5, 12, 18, 28.
    """

    RATE_CHOICES = [
        (Decimal("0.00"), "0%"),
        (Decimal("5.00"), "5%"),
        (Decimal("12.00"), "12%"),
        (Decimal("18.00"), "18%"),
        (Decimal("28.00"), "28%"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, unique=True)
    rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        help_text="GST rate as a percentage (e.g. 18.00 means 18%).",
    )
    cgst_ledger = models.ForeignKey(
        Ledger,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cgst_rate_mappings",
    )
    sgst_ledger = models.ForeignKey(
        Ledger,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sgst_rate_mappings",
    )
    igst_ledger = models.ForeignKey(
        Ledger,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="igst_rate_mappings",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "GST Rate Ledger Mapping"
        verbose_name_plural = "GST Rate Ledger Mappings"
        unique_together = (("organization", "rate"),)
        ordering = ["rate"]

    def __str__(self) -> str:
        return f"{self.organization.name} · {self.rate}%"


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
    file = models.FileField(upload_to=bill_upload_path, validators=[validate_file_extension, validate_bill_file_size])
    file_type = models.CharField(
        choices=BillType.choices, max_length=100, blank=True, null=True, default=BillType.SINGLE
    )
    analysed_data = models.JSONField(default=dict, blank=True, null=True)
    status = models.CharField(
        max_length=10, choices=BillStatus.choices, default=BillStatus.DRAFT, blank=True
    )
    process = models.BooleanField(default=False, help_text=("HAS-been-analysed flag: True once AI analysis has produced ``analysed_data`` and an ``analysed_headers`` row. Semantically means \"analysis done\", not \"currently processing\". For the in-progress state see ``is_processing``. Historical name — keep for backwards compat."))
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="tally_vendor_bills",
        null=True,
        blank=True,
        help_text="User who uploaded this vendor bill"
    )

    # Duplicate tracking fields
    is_duplicate = models.BooleanField(default=False, help_text="Whether this bill is identified as a potential duplicate")
    duplicate_description = models.TextField(blank=True, null=True, help_text="Description of duplicate matches found")
    duplicate_score = models.FloatField(null=True, blank=True, help_text="Similarity score with duplicate bills (0-100)")
    duplicate_matched_bills = models.JSONField(default=list, blank=True, help_text="List of matched duplicate bill details")

    # Processing status fields
    is_processing = models.BooleanField(default=False, help_text="Whether this bill is currently being processed in background")
    processing_error = models.TextField(blank=True, null=True, help_text="Error message if processing failed")
    job_id = models.CharField(max_length=100, blank=True, null=True, help_text="Background job ID for tracking")
    content_hash = models.CharField(
        max_length=64, blank=True, null=True, db_index=True,
        help_text="SHA-256 hex digest of the uploaded file for exact-duplicate detection",
    )

    # Tally sync status
    tally_synced = models.BooleanField(default=False, help_text="Whether this bill has been synced with Tally")
    tally_sync_message = models.TextField(blank=True, null=True, help_text="Message from Tally about sync result (success or error reason)")

    # Bill ownership and description
    bill_belong_your_org = models.BooleanField(default=False, help_text="Whether this bill belongs to your organization")
    description = models.TextField(blank=True, null=True, help_text="Description of the bill")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Tally Vendor Bill"
        verbose_name_plural = "Tally Vendor Bills"

    def __str__(self) -> str:
        return self.bill_munshi_name or f"TallyVendorBill:{self.id}"

    def save(self, *args, **kwargs):
        """
        Autogenerate bill_munshi_name as 'YYYYMMDDTB{N}' if missing.
        Also validates status transitions.
        """
        if not self.bill_munshi_name:
            from datetime import date
            today = date.today()
            date_prefix = today.strftime("%Y%m%d")
            bill_prefix = f"{date_prefix}TB"
            
            # Get all existing bills with today's date prefix for this organization
            existing_bills = TallyVendorBill.objects.filter(
                organization=self.organization,
                bill_munshi_name__startswith=bill_prefix
            ).values_list('bill_munshi_name', flat=True)

            # Extract numbers and find the maximum for today
            max_num = 0
            pattern = rf"{re.escape(bill_prefix)}(\d+)$"
            for bill_name in existing_bills:
                if bill_name:
                    m = re.match(pattern, bill_name)
                    if m:
                        num = int(m.group(1))
                        max_num = max(max_num, num)

            next_num = max_num + 1
            self.bill_munshi_name = f"{bill_prefix}{next_num:05d}"  # 5-digit padding

        super().save(*args, **kwargs)


class TallyVendorAnalyzedBill(BaseOrgModel):
    class TaxType(models.TextChoices):
        TCS = "TCS", "is_tcs_tax"
        TDS = "TDS", "is_tds_tax"

    class GSTType(models.TextChoices):
        IGST = "IGST", "IGST"
        CGST_SGST = "CGST_SGST", "CGST+SGST"
        UNKNOWN = "Unknown", "Unknown"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, unique=True)
    # ``selected_bill`` is 1:1 semantically — one AnalyzedBill per parent
    # ``TallyVendorBill``. Enforced at DB level via ``UniqueConstraint`` in
    # ``Meta.constraints`` below so the double-analysis race (sync analyze
    # button + async RQ job creating twin rows) can no longer create
    # duplicates. Sync callers should use ``get_or_create`` to be idempotent.
    selected_bill = models.ForeignKey(
        TallyVendorBill, on_delete=models.CASCADE, blank=True, null=True, related_name="analysed_headers"
    )
    vendor = models.ForeignKey(
        Ledger, on_delete=models.CASCADE, blank=True, null=True, related_name="vendor_tally_vendor_analysed_bills"
    )

    bill_no = models.CharField(max_length=50, blank=True, null=True)
    bill_date = models.DateField(blank=True, null=True)
    due_date = models.DateField(blank=True, null=True)

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

    discount = models.DecimalField(max_digits=15, decimal_places=2, blank=True, null=True, default=Decimal("0"))
    discount_taxes = models.ForeignKey(
        Ledger, on_delete=models.CASCADE, blank=True, null=True, related_name="discount_tally_vendor_analysed_bills"
    )

    cess = models.DecimalField(max_digits=15, decimal_places=2, blank=True, null=True, default=Decimal("0"))
    cess_taxes = models.ForeignKey(
        Ledger, on_delete=models.CASCADE, blank=True, null=True, related_name="cess_tally_vendor_analysed_bills"
    )

    freight = models.DecimalField(max_digits=15, decimal_places=2, blank=True, null=True, default=Decimal("0"))
    freight_taxes = models.ForeignKey(
        Ledger, on_delete=models.CASCADE, blank=True, null=True, related_name="freight_tally_vendor_analysed_bills"
    )

    round_off = models.DecimalField(max_digits=15, decimal_places=2, blank=True, null=True, default=Decimal("0"))
    round_off_taxes = models.ForeignKey(
        Ledger, on_delete=models.CASCADE, blank=True, null=True, related_name="round_off_tally_vendor_analysed_bills"
    )

    gst_type = models.CharField(max_length=20, choices=GSTType.choices, default=GSTType.UNKNOWN)
    note = models.TextField(blank=True, null=True, default="Enter Your Description")

    # Line Items Consolidation Setting
    consolidate = models.BooleanField(
        default=False,
        help_text="If True, sync as single consolidated line item. If False, sync all individual line items."
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Tally Vendor Analysed Bill"
        verbose_name_plural = "Tally Vendor Analysed Bills"
        constraints = [
            # One AnalyzedBill per parent TallyVendorBill. Closes the
            # sync-analyze + async-RQ double-create race.
            models.UniqueConstraint(
                fields=["selected_bill"],
                name="uq_tally_vendor_analyzed_selected_bill",
                condition=models.Q(selected_bill__isnull=False),
            ),
        ]

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

    # Threshold below which a residual is treated as a rounding artefact rather
    # than a data-entry error. Beyond this we leave round_off at zero so the
    # mismatch surfaces during review.
    ROUND_OFF_THRESHOLD = Decimal("1.00")

    def compute_round_off(self, save=True):
        """Recompute the round_off amount for this vendor bill.

        Formula: subtotal(items) + igst + cgst + sgst + cess + freight - discount
                 + round_off  ==  total
        ⇒ round_off = total - (subtotal + igst + cgst + sgst + cess + freight - discount)

        Only applied when |round_off| < ROUND_OFF_THRESHOLD (≤ ₹1). Beyond that
        the residual indicates an OCR or entry mismatch and round_off stays 0.

        Auto-picks `round_off_taxes` from the first ledger under any parent
        in `TallyConfig.round_off_parents` if not already set.
        """
        from decimal import Decimal as _D
        subtotal = sum(
            (p.amount or _D("0")) for p in self.products.all()
        ) if self.pk else _D("0")
        igst = self.igst or _D("0")
        cgst = self.cgst or _D("0")
        sgst = self.sgst or _D("0")
        cess = self.cess or _D("0")
        freight = self.freight or _D("0")
        discount = self.discount or _D("0")
        total = self.total or _D("0")

        expected = subtotal + igst + cgst + sgst + cess + freight - discount
        diff = (total - expected).quantize(_D("0.01"))

        if abs(diff) < self.ROUND_OFF_THRESHOLD:
            self.round_off = diff
        else:
            self.round_off = _D("0")

        if self.round_off and self.round_off != _D("0") and not self.round_off_taxes_id:
            org = getattr(self, 'organization', None) if self.organization_id else None
            self.round_off_taxes = _pick_round_off_ledger(org)

        if save:
            self.save(skip_validation=True, update_fields=['round_off', 'round_off_taxes'])
        return self.round_off


class TallyVendorAnalyzedProduct(BaseOrgModel):
    """
    Analyzed products from vendor bills.
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

    item_name = models.CharField(max_length=2000, blank=True, null=True)
    item_details = models.TextField(blank=True, null=True)
    taxes = models.ForeignKey(Ledger, on_delete=models.CASCADE, blank=True, null=True)

    price = models.DecimalField(max_digits=50, decimal_places=2, blank=True, null=True)
    quantity = models.PositiveIntegerField(blank=True, null=True, default=0)
    amount = models.DecimalField(max_digits=50, decimal_places=2, blank=True, null=True)

    product_gst = models.CharField(max_length=50, choices=GST_CHOICES, blank=True, null=True)
    igst = models.DecimalField(max_digits=50, decimal_places=2, blank=True, null=True, default=Decimal("0"))
    cgst = models.DecimalField(max_digits=50, decimal_places=2, blank=True, null=True, default=Decimal("0"))
    sgst = models.DecimalField(max_digits=50, decimal_places=2, blank=True, null=True, default=Decimal("0"))

    cgst_ledger = models.ForeignKey(
        Ledger,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="cgst_tally_vendor_products",
        help_text="Per-line CGST tax ledger (resolved from rate mapping or chosen manually).",
    )
    sgst_ledger = models.ForeignKey(
        Ledger,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="sgst_tally_vendor_products",
        help_text="Per-line SGST tax ledger.",
    )
    igst_ledger = models.ForeignKey(
        Ledger,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="igst_tally_vendor_products",
        help_text="Per-line IGST tax ledger.",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Tally Analysed Bill Product"
        verbose_name_plural = "Tally Analysed Bill Products"

    def __str__(self) -> str:
        return self.item_name or f"VendorProduct:{self.id}"

    @property
    def gst_rate_decimal(self):
        """Parse `product_gst` text ('18%', '5%', 'Exempted', 'N/A') to a Decimal rate.
        Returns Decimal('0') for Exempted / N/A / unparseable values.
        """
        if not self.product_gst:
            return Decimal("0")
        m = re.match(r"^\s*([\d.]+)\s*%?\s*$", str(self.product_gst))
        if not m:
            return Decimal("0")
        try:
            return Decimal(m.group(1))
        except Exception:
            return Decimal("0")


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
    file = models.FileField(upload_to=bill_upload_path, validators=[validate_file_extension, validate_bill_file_size])
    file_type = models.CharField(  # Fixed: was fileType
        choices=BillType.choices, max_length=100, blank=True, null=True, default=BillType.SINGLE
    )
    analysed_data = models.JSONField(default=dict, blank=True, null=True)
    status = models.CharField(
        max_length=10, choices=BillStatus.choices, default=BillStatus.DRAFT, blank=True
    )
    process = models.BooleanField(default=False, help_text=("HAS-been-analysed flag: True once AI analysis has produced ``analysed_data`` and an ``analysed_headers`` row. Semantically means \"analysis done\", not \"currently processing\". For the in-progress state see ``is_processing``. Historical name — keep for backwards compat."))
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="tally_expense_bills",
        null=True,
        blank=True,
        help_text="User who uploaded this expense bill"
    )

    # Duplicate tracking fields
    is_duplicate = models.BooleanField(default=False, help_text="Whether this bill is identified as a potential duplicate")
    duplicate_description = models.TextField(blank=True, null=True, help_text="Description of duplicate matches found")
    duplicate_score = models.FloatField(null=True, blank=True, help_text="Similarity score with duplicate bills (0-100)")
    duplicate_matched_bills = models.JSONField(default=list, blank=True, help_text="List of matched duplicate bill details")

    # Processing status fields
    is_processing = models.BooleanField(default=False, help_text="Whether this bill is currently being processed in background")
    processing_error = models.TextField(blank=True, null=True, help_text="Error message if processing failed")
    job_id = models.CharField(max_length=100, blank=True, null=True, help_text="Background job ID for tracking")
    content_hash = models.CharField(
        max_length=64, blank=True, null=True, db_index=True,
        help_text="SHA-256 hex digest of the uploaded file for exact-duplicate detection",
    )

    # Tally sync status
    tally_synced = models.BooleanField(default=False, help_text="Whether this bill has been synced with Tally")
    tally_sync_message = models.TextField(blank=True, null=True, help_text="Message from Tally about sync result (success or error reason)")

    # Bill ownership and description
    bill_belong_your_org = models.BooleanField(default=False, help_text="Whether this bill belongs to your organization")
    description = models.TextField(blank=True, null=True, help_text="Description of the bill")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Tally Expense Bill"
        verbose_name_plural = "Tally Expense Bills"

    def __str__(self) -> str:
        return self.bill_munshi_name or f"TallyExpenseBill:{self.id}"

    def save(self, *args, **kwargs):
        """
        Autogenerate bill_munshi_name as 'YYYYMMDDТЕ{N}' if missing.
        """
        if not self.bill_munshi_name:
            from datetime import date
            today = date.today()
            date_prefix = today.strftime("%Y%m%d")
            bill_prefix = f"{date_prefix}TE"
            
            # Get all existing bills with today's date prefix for this organization
            existing_bills = TallyExpenseBill.objects.filter(
                organization=self.organization,
                bill_munshi_name__startswith=bill_prefix
            ).values_list('bill_munshi_name', flat=True)

            # Extract numbers and find the maximum for today
            max_num = 0
            pattern = rf"{re.escape(bill_prefix)}(\d+)$"
            for bill_name in existing_bills:
                if bill_name:
                    m = re.match(pattern, bill_name)
                    if m:
                        num = int(m.group(1))
                        max_num = max(max_num, num)

            next_num = max_num + 1
            self.bill_munshi_name = f"{bill_prefix}{next_num:05d}"  # 5-digit padding

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
    vendor_amount = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True, default=Decimal("0"))

    voucher = models.CharField(max_length=255, blank=True, null=True)
    bill_no = models.CharField(max_length=50, blank=True, null=True)
    bill_date = models.DateField(blank=True, null=True)
    due_date = models.DateField(blank=True, null=True)
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

    # TDS Fields
    tds = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True, default=Decimal("0"))
    tds_taxes = models.ForeignKey(
        Ledger, on_delete=models.CASCADE, blank=True, null=True, related_name="tds_tally_expense_analysed_bills"
    )
    tds_debit_or_credit = models.CharField(
        choices=DebitCredit.choices, max_length=10, blank=True, null=True, default=DebitCredit.DEBIT
    )

    # Other Adjustment Fields
    other_adjustment = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True, default=Decimal("0"))
    other_adjustment_taxes = models.ForeignKey(
        Ledger, on_delete=models.CASCADE, blank=True, null=True, related_name="other_adjustment_tally_expense_analysed_bills"
    )
    other_adjustment_debit_or_credit = models.CharField(
        choices=DebitCredit.choices, max_length=10, blank=True, null=True, default=DebitCredit.DEBIT
    )

    # Round Off Fields
    round_off = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True, default=Decimal("0"))
    round_off_taxes = models.ForeignKey(
        Ledger, on_delete=models.CASCADE, blank=True, null=True, related_name="round_off_tally_expense_analysed_bills"
    )
    round_off_debit_or_credit = models.CharField(
        choices=DebitCredit.choices, max_length=10, blank=True, null=True, default=DebitCredit.DEBIT
    )

    note = models.CharField(max_length=100, blank=True, null=True, default="Enter Your Description")

    # Line Items Consolidation Setting
    consolidate = models.BooleanField(
        default=False,
        help_text="If True, sync as single consolidated expense entry. If False, sync all individual entries."
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Tally Expense Analysed Bill"
        verbose_name_plural = "Tally Expense Analysed Bills"
        constraints = [
            # One AnalyzedBill per parent TallyExpenseBill — closes the
            # sync-analyze + async-RQ double-create race (see #4 audit).
            models.UniqueConstraint(
                fields=["selected_bill"],
                name="uq_tally_expense_analyzed_selected_bill",
                condition=models.Q(selected_bill__isnull=False),
            ),
        ]

    def __str__(self) -> str:
        return (self.selected_bill.bill_munshi_name if self.selected_bill else None) or f"ExpenseAnalysed:{self.id}"

    def save(self, *args, **kwargs):
        # Handle skip_validation parameter for compatibility
        skip_validation = kwargs.pop('skip_validation', False)
        if not skip_validation:
            self.full_clean()
        super().save(*args, **kwargs)

    ROUND_OFF_THRESHOLD = Decimal("1.00")

    def compute_round_off(self, save=True):
        """Recompute round_off for an expense (journal) bill.

        Formula (DR == CR rule):
            DR = sum(debit-side line items) + sum(debit-side tax & adjustment amounts)
            CR = sum(credit-side line items) + sum(credit-side tax & adjustment amounts)
                 + (vendor_amount when vendor_debit_or_credit == 'credit')
            (vendor_amount on the debit side is added to DR analogously.)

        Residual = DR - CR. Only applied when |Residual| < ROUND_OFF_THRESHOLD.
        Sign decides which side the round_off entry sits on:
          • Residual > 0  → CR side gains the round_off (to balance)
          • Residual < 0  → DR side gains the round_off
        round_off is always stored as a positive magnitude;
        round_off_debit_or_credit indicates the side.
        """
        from decimal import Decimal as _D

        def _sum_side(side):
            total = _D("0")
            if self.pk:
                for product in self.products.all():
                    if product.debit_or_credit == side:
                        total += product.amount or _D("0")
            for amount, dc in (
                (self.igst, self.igst_debit_or_credit),
                (self.cgst, self.cgst_debit_or_credit),
                (self.sgst, self.sgst_debit_or_credit),
                (self.tds, self.tds_debit_or_credit),
                (self.other_adjustment, self.other_adjustment_debit_or_credit),
                (self.vendor_amount, self.vendor_debit_or_credit),
            ):
                if dc == side and amount:
                    total += amount
            return total

        dr_total = _sum_side(self.DebitCredit.DEBIT)
        cr_total = _sum_side(self.DebitCredit.CREDIT)
        residual = (dr_total - cr_total).quantize(_D("0.01"))

        if abs(residual) >= self.ROUND_OFF_THRESHOLD or residual == _D("0"):
            self.round_off = _D("0")
        else:
            self.round_off = abs(residual)
            self.round_off_debit_or_credit = (
                self.DebitCredit.CREDIT if residual > 0 else self.DebitCredit.DEBIT
            )
            if not self.round_off_taxes_id:
                org = getattr(self, 'organization', None) if self.organization_id else None
                self.round_off_taxes = _pick_round_off_ledger(org)

        if save:
            self.save(
                skip_validation=True,
                update_fields=['round_off', 'round_off_debit_or_credit', 'round_off_taxes'],
            )
        return self.round_off


class TallyExpenseAnalyzedProduct(BaseOrgModel):
    class DebitCredit(models.TextChoices):
        CREDIT = "credit", "Credit"
        DEBIT = "debit", "Debit"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, unique=True)
    expense_bill = models.ForeignKey(
        TallyExpenseAnalyzedBill, related_name="products", on_delete=models.CASCADE
    )

    item_details = models.CharField(max_length=2000, blank=True, null=True)
    chart_of_accounts = models.ForeignKey(Ledger, on_delete=models.CASCADE, blank=True, null=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True, default=Decimal("0"))
    debit_or_credit = models.CharField(
        choices=DebitCredit.choices, max_length=50, blank=True, null=True, default=DebitCredit.DEBIT
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Tally Expense Analysed Bill Product"
        verbose_name_plural = "Tally Expense Analysed Bill Products"

    def __str__(self) -> str:
        if self.expense_bill and self.expense_bill.selected_bill:
            return self.expense_bill.selected_bill.bill_munshi_name or f"ExpenseProduct:{self.id}"
        return f"ExpenseProduct:{self.id}"


# -----------------------------
# Consolidated Products for Line Items Consolidation
# -----------------------------

class TallyVendorConsolidatedProduct(BaseOrgModel):
    """
    Represents a consolidated product line item for Tally vendor bills.
    Used when consolidate=True in TallyVendorAnalyzedBill to avoid ledger conflicts.
    Contains aggregated data from multiple TallyVendorAnalyzedProduct entries.
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

    id = models.UUIDField(default=uuid.uuid4, unique=True, primary_key=True, editable=False)
    vendor_bill_analyzed = models.ForeignKey("TallyVendorAnalyzedBill", on_delete=models.CASCADE, related_name="consolidated_products")

    # 📦 SAME FIELDS AS TallyVendorAnalyzedProduct (for compatibility)
    item_name = models.CharField(
        max_length=2000,
        blank=True,
        null=True,
        help_text="Consolidated item name"
    )
    item_details = models.TextField(
        blank=True,
        null=True,
        help_text="Detailed breakdown of consolidated items"
    )
    taxes = models.ForeignKey(
        Ledger,
        on_delete=models.CASCADE,
        blank=True,
        null=True,
        help_text="Primary tax ledger for consolidated item"
    )

    # Financial data (same field names as individual products)
    price = models.DecimalField(
        max_digits=50,
        decimal_places=2,
        blank=True,
        null=True,
        help_text="Consolidated rate/price"
    )
    quantity = models.PositiveIntegerField(
        blank=True,
        null=True,
        default=1,
        help_text="Usually 1 for consolidated items"
    )
    amount = models.DecimalField(
        max_digits=50,
        decimal_places=2,
        blank=True,
        null=True,
        help_text="Total consolidated amount"
    )

    # 💰 GST fields (same as individual products)
    product_gst = models.CharField(
        max_length=50,
        choices=GST_CHOICES,
        blank=True,
        null=True,
        help_text="Most common GST rate from consolidated items"
    )
    igst = models.DecimalField(
        max_digits=50,
        decimal_places=2,
        blank=True,
        null=True,
        default=Decimal("0"),
        help_text="Total IGST amount"
    )
    cgst = models.DecimalField(
        max_digits=50,
        decimal_places=2,
        blank=True,
        null=True,
        default=Decimal("0"),
        help_text="Total CGST amount"
    )
    sgst = models.DecimalField(
        max_digits=50,
        decimal_places=2,
        blank=True,
        null=True,
        default=Decimal("0"),
        help_text="Total SGST amount"
    )

    # 📊 Metadata fields
    original_items_count = models.IntegerField(
        default=0,
        help_text="Number of original line items that were consolidated"
    )
    consolidation_notes = models.TextField(
        null=True,
        blank=True,
        help_text="Notes about how consolidation was performed"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Consolidated Tally Vendor Product"
        verbose_name_plural = "Consolidated Tally Vendor Products"

    def __str__(self):
        return f"Consolidated: {self.item_name or 'Multiple Items'} ({self.original_items_count} items)"


class TallyExpenseConsolidatedProduct(BaseOrgModel):
    """
    Represents a consolidated product line item for Tally expense bills.
    Used when consolidate=True in TallyExpenseAnalyzedBill to avoid ledger conflicts.
    Contains aggregated data from multiple TallyExpenseAnalyzedProduct entries.
    """
    class DebitCredit(models.TextChoices):
        CREDIT = "credit", "Credit"
        DEBIT = "debit", "Debit"

    id = models.UUIDField(default=uuid.uuid4, unique=True, primary_key=True, editable=False)
    expense_bill = models.ForeignKey("TallyExpenseAnalyzedBill", on_delete=models.CASCADE, related_name="consolidated_products")

    # 📦 SAME FIELDS AS TallyExpenseAnalyzedProduct (for compatibility)
    item_details = models.CharField(
        max_length=2000,
        blank=True,
        null=True,
        help_text="Consolidated expense item details"
    )
    chart_of_accounts = models.ForeignKey(
        Ledger,
        on_delete=models.CASCADE,
        blank=True,
        null=True,
        help_text="Primary chart of accounts for consolidated entry"
    )
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        blank=True,
        null=True,
        default=Decimal("0"),
        help_text="Total consolidated amount"
    )
    debit_or_credit = models.CharField(
        choices=DebitCredit.choices,
        max_length=50,
        blank=True,
        null=True,
        default=DebitCredit.DEBIT,
        help_text="Debit or credit for consolidated entry"
    )

    # 📊 Metadata fields
    original_entries_count = models.IntegerField(
        default=0,
        help_text="Number of original expense entries that were consolidated"
    )
    consolidation_notes = models.TextField(
        null=True,
        blank=True,
        help_text="Notes about how consolidation was performed"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Consolidated Tally Expense Product"
        verbose_name_plural = "Consolidated Tally Expense Products"

    def __str__(self):
        return f"Consolidated Expense: {self.item_details[:50] if self.item_details else 'Multiple Entries'} ({self.original_entries_count} entries)"


# -----------------------------
# GST Lines for Expense (Journal) bills
# -----------------------------

class TallyExpenseGstLine(BaseOrgModel):
    """
    Per-rate GST entry on a Tally expense (Journal) bill.

    Each row maps 1:1 to a ``<ledger>`` entry inside the sync XML's
    ``<ledgers>`` block. A bill can have N rows: one per distinct
    (rate, tax_type, ledger) combination. This is how multi-rate GST
    is modelled on the expense side (vendor bill achieves the same
    via per-line product GST + a rollup; expense items don't carry
    GST so it's stored explicitly here at bill level).

    Example: an 18% intrastate bill has 2 rows (CGST 18% + SGST 18%).
    A bill with 18% and 28% items has 4 rows.
    """

    class TaxType(models.TextChoices):
        CGST = "CGST", "CGST"
        SGST = "SGST", "SGST"
        IGST = "IGST", "IGST"

    class DebitCredit(models.TextChoices):
        DEBIT = "debit", "Debit"
        CREDIT = "credit", "Credit"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, unique=True)
    expense_bill = models.ForeignKey(
        TallyExpenseAnalyzedBill,
        on_delete=models.CASCADE,
        related_name="gst_lines",
    )
    rate = models.CharField(
        max_length=10,
        blank=True,
        default="",
        help_text="GST rate string like '18%', '28%'. Informational on Tally side.",
    )
    tax_type = models.CharField(
        max_length=10,
        choices=TaxType.choices,
        help_text="CGST / SGST / IGST. Determines which ledger pool the dropdown shows.",
    )
    amount = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal("0"),
    )
    ledger = models.ForeignKey(
        Ledger,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="tally_expense_gst_lines",
        help_text="The CGST/SGST/IGST tax ledger this amount posts against in Tally.",
    )
    debit_or_credit = models.CharField(
        max_length=10,
        choices=DebitCredit.choices,
        default=DebitCredit.DEBIT,
        help_text="Usually debit (input credit). Flips to credit for RCM payable entries.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Tally Expense GST Line"
        verbose_name_plural = "Tally Expense GST Lines"
        ordering = ["rate", "tax_type"]

    def __str__(self) -> str:
        return f"{self.tax_type} {self.rate} ₹{self.amount} ({self.debit_or_credit})"


# -----------------------------
# Tally Setup Guide (admin-managed walkthrough shown on the Tally Account Info page)
# -----------------------------


class TallySetupStep(models.Model):
    """
    A single step in the Tally setup guide. Edited from Django admin so the
    integration walkthrough can be updated without a frontend deploy.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, unique=True)
    step_number = models.PositiveSmallIntegerField(
        help_text="Step number shown to the user (1, 2, 3, ...)"
    )
    title = models.CharField(max_length=200)
    description = models.TextField(
        help_text="Plain text or markdown — supports newlines."
    )
    image = models.ImageField(
        upload_to="tally/setup-guide/",
        blank=True,
        null=True,
        help_text="Optional screenshot for this step.",
    )
    image_alt = models.CharField(
        max_length=200,
        blank=True,
        help_text="Alt text for accessibility — describe the screenshot.",
    )
    order = models.PositiveSmallIntegerField(
        default=0,
        help_text="Tiebreaker when two steps share a step_number (lower comes first).",
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Hide a step without deleting it.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["step_number", "order"]
        verbose_name = "Tally Setup Step"
        verbose_name_plural = "Tally Setup Steps"
        indexes = [
            models.Index(fields=["is_active", "step_number"]),
        ]

    def __str__(self):
        return f"Step {self.step_number}: {self.title}"


class TallyTcpRelease(models.Model):
    """
    Versioned Tally TCP file. The file marked is_active=True is what users get
    when they click 'Download TCP' on the Account Info page. Older versions
    are kept for audit / rollback.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, unique=True)
    version = models.CharField(
        max_length=50,
        unique=True,
        help_text="Semantic version, e.g. '1.2.3'.",
    )
    file = models.FileField(
        upload_to="tally/tcp/",
        help_text="Upload the .tcp file here. The latest active release will be served to users.",
    )
    notes = models.TextField(
        blank=True,
        help_text="Optional release notes (visible only in admin).",
    )
    is_active = models.BooleanField(
        default=False,
        help_text="Only one release should be active at a time. "
                  "Saving a release with this checked will deactivate all others.",
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tally_tcp_releases",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Tally TCP Release"
        verbose_name_plural = "Tally TCP Releases"
        indexes = [
            models.Index(fields=["is_active", "-created_at"]),
        ]

    def save(self, *args, **kwargs):
        # If this release is being marked active, deactivate every other one.
        super().save(*args, **kwargs)
        if self.is_active:
            type(self).objects.filter(is_active=True).exclude(pk=self.pk).update(is_active=False)

    def __str__(self):
        marker = " (active)" if self.is_active else ""
        return f"Tally TCP v{self.version}{marker}"

# apps/zoho/models.py

import re
import uuid

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from django.db.models import Q

from apps.organizations.models import Organization
from apps.common.bill_naming import (
    NAME_UNIQUENESS_ENFORCED_FROM,
    save_with_unique_name,
)
from apps.common.validators import (
    bill_upload_path,
    validate_bill_file_size,
    validate_file_extension,
)

# Constraint names are referenced both by Meta and by the retry logic (which
# matches them against the IntegrityError message), so they live in one place.
UNIQUE_ZOHO_VENDOR_BILL_NAME = "uniq_zoho_vendorbill_org_name"
UNIQUE_ZOHO_JOURNAL_BILL_NAME = "uniq_zoho_journalbill_org_name"
UNIQUE_ZOHO_EXPENSE_BILL_NAME = "uniq_zoho_expensebill_org_name"


# -----------------------------
# Helpers / Base
# -----------------------------


class BaseTeamModel(models.Model):
    """
    Common base for all Zoho models in this app.
    Ensures each record is scoped to a specific Organization.
    """
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="zoho_%(class)ss",
    )

    class Meta:
        abstract = True


# ---------------------------
# Zoho Settings / Credentials
# ---------------------------

class ZohoCredentials(BaseTeamModel):
    """
    Stores authentication credentials and tokens for connecting to the Zoho Books API.
    Handles token refresh and validation functionality.
    """
    id = models.UUIDField(default=uuid.uuid4, unique=True, primary_key=True, editable=False)
    clientId = models.CharField(max_length=100)
    clientSecret = models.CharField(max_length=100)
    accessCode = models.CharField(max_length=200, default="Your Access Code")
    organisationId = models.CharField(max_length=100, default="Your organisationId")
    redirectUrl = models.CharField(max_length=200, default="Your Redirect URL")
    accessToken = models.CharField(max_length=200, null=True, blank=True)
    refreshToken = models.CharField(max_length=200, null=True, blank=True)
    token_expiry = models.DateTimeField(null=True, blank=True)
    is_connected = models.BooleanField(default=False, help_text="Whether the Zoho Books integration is fully connected and functional")
    created_at = models.DateTimeField(auto_now_add=True)
    update_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Zoho Credential"
        verbose_name_plural = "Zoho Credentials"

    def __str__(self):
        return f"{self.organization.name} · ZohoCredentials"

    def is_token_valid(self):
        """Check if the current access token is still valid"""
        if not self.accessToken or not self.token_expiry:
            return False
        return timezone.now() < self.token_expiry

    def refresh_token(self):
        """Refresh the access token using the refresh token"""
        if not self.refreshToken:
            logger.error("[ERROR] No refresh token available")
            return False
        
        import logging
        logger = logging.getLogger(__name__)
        
        logger.debug(f"[DEBUG] Starting token refresh for organization ID: {self.organisationId}")
        logger.info(f"Starting token refresh for organization ID: {self.organisationId}")

        # Build refresh token URL
        url = (
            "https://accounts.zoho.in/oauth/v2/token"
            f"?refresh_token={self.refreshToken}"
            f"&client_id={self.clientId}"
            f"&client_secret={self.clientSecret}"
            f"&grant_type=refresh_token"
        )
        
        logger.debug(f"[DEBUG] Refresh token URL: {url[:80]}...[MASKED]")
        logger.info(f"Refresh token request initiated")

        try:
            import requests
            response = requests.post(url, timeout=30)
            
            logger.debug(f"[DEBUG] Refresh token response status: {response.status_code}")
            logger.info(f"Refresh token response status: {response.status_code}")

            if response.status_code == 200:
                data = response.json()
                logger.debug(f"[DEBUG] Refresh token response keys: {list(data.keys())}")
                logger.info(f"Refresh token response received: {list(data.keys())}")
                
                if "access_token" in data:
                    old_token = self.accessToken[:20] + "..." if self.accessToken else "None"
                    self.accessToken = data["access_token"]
                    new_token = self.accessToken[:20] + "..."
                    
                    logger.debug(f"[DEBUG] Access token updated: {old_token} -> {new_token}")
                    logger.info(f"Access token refreshed successfully")
                    
                    # Set expiry to 50 minutes from now (Zoho tokens last 1 hour)
                    self.token_expiry = timezone.now() + timezone.timedelta(minutes=50)
                    
                    # Update connection status
                    self.is_connected = bool(self.accessToken and self.organisationId)
                    
                    logger.debug(f"[DEBUG] Token expiry set to: {self.token_expiry}")
                    logger.debug(f"[DEBUG] Connection status: {self.is_connected}")
                    
                    self.save(update_fields=["accessToken", "token_expiry", "is_connected", "update_at"])
                    
                    logger.info(f"[SUCCESS] Token refresh completed successfully")
                    logger.info(f"Token refresh completed successfully")
                    return True
                else:
                    logger.error(f"[ERROR] No access_token in response: {data}")
                    logger.error(f"No access_token in refresh response: {data}")
            else:
                error_text = response.text
                logger.error(f"[ERROR] Refresh token failed: {response.status_code} - {error_text}")
                logger.error(f"Failed to refresh Zoho token: {response.status_code} - {error_text}")
                
                # Try to parse error response
                try:
                    error_data = response.json()
                    logger.error(f"[ERROR] Zoho error details: {error_data}")
                    logger.error(f"Zoho refresh error details: {error_data}")
                except:
                    pass
                    
        except Exception as e:
            logger.error(f"[EXCEPTION] Error refreshing Zoho token: {str(e)}")
            logger.exception(f"Exception refreshing Zoho token: {str(e)}")

        logger.error(f"[FAILED] Token refresh failed")
        logger.error(f"Token refresh failed")
        return False


# --------------
# Zoho Vendors
# --------------

class ZohoVendor(BaseTeamModel):
    """
    Stores vendor information synchronized from Zoho Books.
    Used for associating bills with specific vendors.
    """
    id = models.UUIDField(default=uuid.uuid4, unique=True, primary_key=True, editable=False)
    contactId = models.CharField(max_length=100)
    companyName = models.CharField(max_length=100)
    gstNo = models.CharField(max_length=30)
    gst_treatment = models.CharField(max_length=100, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Zoho Vendor"
        verbose_name_plural = "Zoho Vendors"

    def __str__(self):
        return self.companyName


# -----------------------
# Zoho Chart of Accounts
# -----------------------

class ZohoChartOfAccount(BaseTeamModel):
    """
    Stores chart of accounts information synchronized from Zoho Books.
    Used for categorizing expenses and bill items.
    """
    id = models.UUIDField(default=uuid.uuid4, unique=True, primary_key=True, editable=False)
    accountId = models.CharField(max_length=100)
    accountName = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Zoho Chart of Account"
        verbose_name_plural = "Zoho Chart of Accounts"

    def __str__(self):
        return self.accountName


# -----------
# Zoho Taxes
# -----------

class ZohoTaxes(BaseTeamModel):
    """
    Stores tax information synchronized from Zoho Books.
    Used for applying the correct tax rates to bill items.
    """
    id = models.UUIDField(default=uuid.uuid4, unique=True, primary_key=True, editable=False)
    taxId = models.CharField(max_length=100)
    taxName = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Zoho Tax"
        verbose_name_plural = "Zoho Taxes"

    def __str__(self):
        return self.taxName


# -------------------
# Zoho TDS / TCS Tax
# -------------------

class ZohoTdsTcs(BaseTeamModel):
    """
    Manages Tax Deducted at Source (TDS) and Tax Collected at Source (TCS) tax rates
    from Zoho Books for use in vendor bill calculations.
    """
    TAX_CHOICES = (
        ("TCS", "tcs_tax"),
        ("TDS", "tds_tax"),
    )
    id = models.UUIDField(default=uuid.uuid4, unique=True, primary_key=True, editable=False)
    taxId = models.CharField(max_length=100)
    taxName = models.CharField(max_length=100)
    taxPercentage = models.CharField(max_length=100, null=True, blank=True, default=0)
    taxType = models.CharField(choices=TAX_CHOICES, max_length=100, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Zoho TDS/TCS Tax"
        verbose_name_plural = "Zoho TDS and TCS Taxes"

    def __str__(self):
        pct = self.taxPercentage if self.taxPercentage not in (None, "") else "0"
        return f"{self.taxName} ({pct}%)"


# ----------------
# Vendor Credits
# ----------------

class ZohoVendorCredit(BaseTeamModel):
    """
    Stores vendor credit information synchronized from Zoho Books.
    These credits can be applied to vendor bills during bill processing.
    """
    id = models.UUIDField(default=uuid.uuid4, unique=True, primary_key=True, editable=False)
    vendor_id = models.CharField(max_length=100, null=True, blank=True)
    vendor_name = models.CharField(max_length=100, null=True, blank=True)
    vendor_credit_id = models.CharField(max_length=100, null=True, blank=True)
    vendor_credit_number = models.CharField(max_length=100, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Zoho Vendor Credit"
        verbose_name_plural = "Zoho Vendor Credits"

    def __str__(self):
        return self.vendor_name or f"VendorCredit:{self.vendor_credit_id or self.id}"


# ===============================
#         Vendor Bills
# ===============================

class VendorBill(BaseTeamModel):
    """
    Represents a vendor bill/invoice that has been uploaded to the system.
    Tracks the bill file, analysis status, and processing state.
    """
    BILL_STATUS_CHOICES = (
        ("Draft", "Draft"),
        ("Analysed", "Analysed"),
        ("Verified", "Verified"),
        ("Synced", "Synced"),
    )
    BILL_TYPE_CHOICES = (
        ("Single Invoice/File", "Single Invoice/File"),
        ("Multiple Invoice/File", "Multiple Invoice/File"),
    )

    id = models.UUIDField(default=uuid.uuid4, unique=True, primary_key=True, editable=False)
    billmunshiName = models.CharField(max_length=100, null=True, blank=True)
    file = models.FileField(upload_to=bill_upload_path, validators=[validate_file_extension, validate_bill_file_size])
    fileType = models.CharField(choices=BILL_TYPE_CHOICES, max_length=100, null=True, blank=True,
                                default="Single Invoice/File")
    analysed_data = models.JSONField(default=dict, null=True, blank=True)
    status = models.CharField(max_length=10, choices=BILL_STATUS_CHOICES, default="Draft", blank=True)
    process = models.BooleanField(default=False, help_text=("HAS-been-analysed flag: True once AI analysis has produced ``analysed_data`` and an ``analysed_headers`` row. Semantically means \"analysis done\", not \"currently processing\". For the in-progress state see ``is_processing``. Historical name — keep for backwards compat."))
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_vendor_bills"
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

    # Bill ownership and description
    bill_belong_your_org = models.BooleanField(default=False, help_text="Whether this bill belongs to your organization")
    description = models.TextField(blank=True, null=True, help_text="Description of the bill")

    created_at = models.DateTimeField(auto_now_add=True)
    update_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Vendor Bill"
        verbose_name_plural = "Vendor Bills"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "billmunshiName"],
                name=UNIQUE_ZOHO_VENDOR_BILL_NAME,
                # Partial: historical rows keep their (sometimes duplicated) names.
                condition=Q(created_at__gte=NAME_UNIQUENESS_ENFORCED_FROM),
            ),
        ]

    def __str__(self):
        return self.billmunshiName or f"Bill:{self.id}"

    def save(self, *args, **kwargs):
        return save_with_unique_name(
            self,
            super().save,
            name_field="billmunshiName",
            code="ZB",
            constraint=UNIQUE_ZOHO_VENDOR_BILL_NAME,
            should_generate=not self.billmunshiName and bool(self.file),
            args=args,
            kwargs=kwargs,
        )


class VendorZohoBill(BaseTeamModel):
    """
    Represents an analyzed vendor bill with extracted data ready for Zoho Books.
    Links to the original VendorBill and contains tax information and vendor details.
    """
    TAX_TYPE_CHOICES = (
        ("NOT_APPLICABLE", "Not Applicable"),
        ("TCS", "is_tcs_tax"),
        ("TDS", "is_tds_tax"),
    )
    
    # Discount type choices for Zoho Books bill-level discounts
    # INR: Flat amount discount (e.g., ₹100 off)
    # Percentage: Percentage-based discount (e.g., 10% off)
    # Note: Zoho Books always uses 'entity_level' for bill-level discounts
    DISCOUNT_TYPE_CHOICES = (
        ("INR", "INR (Flat Amount)"),
        ("Percentage", "Percentage"),
    )

    id = models.UUIDField(default=uuid.uuid4, unique=True, primary_key=True, editable=False)
    selectBill = models.ForeignKey("VendorBill", on_delete=models.CASCADE, null=True, blank=True)
    vendor = models.ForeignKey("ZohoVendor", on_delete=models.CASCADE, null=True, blank=True)
    bill_no = models.CharField(max_length=50, null=True, blank=True)
    bill_date = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    total = models.CharField(max_length=50, null=True, blank=True, default=0)
    discount_type = models.CharField(choices=DISCOUNT_TYPE_CHOICES, max_length=20, null=True, blank=True, default="Percentage")
    discount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, default=Decimal("0"), help_text="User-entered discount value (percentage or flat amount)")
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, default=Decimal("0"), help_text="Calculated discount amount in INR (auto-calculated)")
    discount_account = models.ForeignKey("ZohoChartOfAccount", on_delete=models.CASCADE, null=True, blank=True, related_name="discount_bills")
    adjustment_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, default=Decimal("0"))
    adjustment_description = models.CharField(max_length=255, null=True, blank=True, default="Adjustment")
    igst = models.CharField(max_length=50, null=True, blank=True, default=0)
    cgst = models.CharField(max_length=50, null=True, blank=True, default=0)
    sgst = models.CharField(max_length=50, null=True, blank=True, default=0)
    tds_tcs_id = models.ForeignKey("ZohoTdsTcs", on_delete=models.CASCADE, null=True, blank=True)
    is_tax = models.CharField(choices=TAX_TYPE_CHOICES, max_length=100, null=True, blank=True, default="NOT_APPLICABLE")
    note = models.CharField(max_length=100, null=True, blank=True, default="Enter Your Description")

    # Line Items Consolidation Setting
    consolidate = models.BooleanField(
        default=False,
        help_text="If True, sync as single consolidated line item. If False, sync all individual line items."
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Analysed Vendor Bill"
        verbose_name_plural = "Analysed Vendor Bills"

    def __str__(self):
        return self.bill_no or (self.selectBill.billmunshiName if self.selectBill else f"ZohoBill:{self.id}")


class VendorZohoProduct(BaseTeamModel):
    """
    Represents a product line item for a vendor bill.
    Contains details about the product including tax information, quantity, rate, and amount.
    """
    ITC_ELIGIBILITY_CHOICES = (
        ("eligible", "Eligible"),
        ("ineligible_section17", "Ineligible Section17"),
        ("ineligible_others", "Ineligible Others"),
    )

    id = models.UUIDField(default=uuid.uuid4, unique=True, primary_key=True, editable=False)
    zohoBill = models.ForeignKey("VendorZohoBill", on_delete=models.CASCADE, related_name="products")
    item_name = models.CharField(max_length=1000, null=True, blank=True)
    item_details = models.CharField(max_length=2000, null=True, blank=True)
    chart_of_accounts = models.ForeignKey("ZohoChartOfAccount", on_delete=models.CASCADE, null=True, blank=True)
    taxes = models.ForeignKey("ZohoTaxes", on_delete=models.CASCADE, null=True, blank=True)
    reverse_charge_tax_id = models.BooleanField(default=False)
    itc_eligibility = models.CharField(
        choices=ITC_ELIGIBILITY_CHOICES,
        max_length=100,
        null=True,
        blank=True,
        default="eligible",
    )
    rate = models.CharField(max_length=50, null=True, blank=True)
    quantity = models.CharField(max_length=50, null=True, blank=True, default=0)
    amount = models.CharField(max_length=50, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Analysed Bill Product"
        verbose_name_plural = "Analysed Bill Products"

    def __str__(self):
        return self.item_name or f"Product:{self.id}"


class VendorZohoConsolidatedProduct(BaseTeamModel):
    """
    Represents a consolidated product line item for vendor bills.
    Used when consolidate=True in VendorZohoBill to avoid tax conflicts.
    Contains aggregated data from multiple VendorZohoProduct entries.
    """
    id = models.UUIDField(default=uuid.uuid4, unique=True, primary_key=True, editable=False)
    zohoBill = models.ForeignKey("VendorZohoBill", on_delete=models.CASCADE, related_name="consolidated_products")

    # Consolidated item details
    consolidated_item_name = models.CharField(
        max_length=1000,
        null=True,
        blank=True,
        default="Multiple items consolidated",
        help_text="Description for the consolidated line item"
    )
    consolidated_item_details = models.TextField(
        null=True,
        blank=True,
        help_text="Detailed breakdown of consolidated items"
    )

    # Financial data
    total_quantity = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        default=Decimal("1"),
        help_text="Total quantity of all items (usually 1 for consolidated)"
    )
    consolidated_rate = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Total amount as rate for consolidated item"
    )
    consolidated_amount = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Total consolidated amount"
    )

    # Tax handling for consolidated item
    chart_of_accounts = models.ForeignKey(
        "ZohoChartOfAccount",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        help_text="Primary chart of accounts for consolidated item"
    )
    taxes = models.ForeignKey(
        "ZohoTaxes",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        help_text="Tax applied to consolidated item (highest tax rate or most common)"
    )

    # ITC and other settings
    itc_eligibility = models.CharField(
        choices=VendorZohoProduct.ITC_ELIGIBILITY_CHOICES,
        max_length=100,
        null=True,
        blank=True,
        default="eligible",
    )
    reverse_charge_tax_id = models.BooleanField(default=False)

    # Metadata
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
        verbose_name = "Consolidated Vendor Product"
        verbose_name_plural = "Consolidated Vendor Products"

    def __str__(self):
        return f"Consolidated: {self.consolidated_item_name} ({self.original_items_count} items)"


# ===============================
#         Journal Bills
# ===============================

class JournalBill(BaseTeamModel):
    """
    Represents an expense bill/invoice that has been uploaded to the system.
    Similar to VendorBill but specifically for expense transactions.
    Tracks the bill file, analysis status, and processing state.
    """
    BILL_STATUS_CHOICES = (
        ("Draft", "Draft"),
        ("Analysed", "Analysed"),
        ("Verified", "Verified"),
        ("Synced", "Synced"),
    )
    BILL_TYPE_CHOICES = (
        ("Single Invoice/File", "Single Invoice/File"),
        ("Multiple Invoice/File", "Multiple Invoice/File"),
    )

    id = models.UUIDField(default=uuid.uuid4, unique=True, primary_key=True, editable=False)
    billmunshiName = models.CharField(max_length=100, null=True, blank=True)
    file = models.FileField(upload_to=bill_upload_path, validators=[validate_file_extension, validate_bill_file_size])
    fileType = models.CharField(choices=BILL_TYPE_CHOICES, max_length=100, null=True, blank=True,
                                default="Single Invoice/File")
    analysed_data = models.JSONField(default=dict, null=True, blank=True)
    status = models.CharField(max_length=10, choices=BILL_STATUS_CHOICES, default="Draft", blank=True)
    process = models.BooleanField(default=False, help_text=("HAS-been-analysed flag: True once AI analysis has produced ``analysed_data`` and an ``analysed_headers`` row. Semantically means \"analysis done\", not \"currently processing\". For the in-progress state see ``is_processing``. Historical name — keep for backwards compat."))
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_journal_bills"
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

    # Bill ownership and description
    bill_belong_your_org = models.BooleanField(default=False, help_text="Whether this bill belongs to your organization")
    description = models.TextField(blank=True, null=True, help_text="Description of the bill")

    created_at = models.DateTimeField(auto_now_add=True)
    update_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Journal Bill"
        verbose_name_plural = "Journal Bills"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "billmunshiName"],
                name=UNIQUE_ZOHO_JOURNAL_BILL_NAME,
                # Partial: historical rows keep their (sometimes duplicated) names.
                condition=Q(created_at__gte=NAME_UNIQUENESS_ENFORCED_FROM),
            ),
        ]

    def __str__(self):
        return self.billmunshiName or f"JournalBill:{self.id}"

    def save(self, *args, **kwargs):
        return save_with_unique_name(
            self,
            super().save,
            name_field="billmunshiName",
            code="ZJ",
            constraint=UNIQUE_ZOHO_JOURNAL_BILL_NAME,
            should_generate=not self.billmunshiName and bool(self.file),
            args=args,
            kwargs=kwargs,
        )


class JournalZohoBill(BaseTeamModel):
    """
    Represents an analyzed expense bill with extracted data ready for Zoho Books.
    Links to the original JournalBill and contains tax information and vendor details.
    """
    TRANSACTION_TYPE_CHOICES = (
        ("credit", "Credit"),
        ("debit", "Debit"),
    )
    id = models.UUIDField(default=uuid.uuid4, unique=True, primary_key=True, editable=False)
    selectBill = models.ForeignKey("JournalBill", on_delete=models.CASCADE, null=True, blank=True)
    vendor = models.ForeignKey("ZohoVendor", on_delete=models.CASCADE, null=True, blank=True)
    bill_no = models.CharField(max_length=50, null=True, blank=True)
    bill_date = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    vendor_coa = models.ForeignKey(
        ZohoChartOfAccount, on_delete=models.CASCADE, blank=True, null=True,
        related_name="vendor_tally_expense_analysed_bills"
    )
    vendor_debit_or_credit = models.CharField(
        choices=TRANSACTION_TYPE_CHOICES, max_length=10, blank=True, null=True, default="credit"
    )
    vendor_amount = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True, default=Decimal("0"))
    total = models.CharField(max_length=50, null=True, blank=True, default=0)
    igst = models.CharField(max_length=50, null=True, blank=True, default=0)
    igst_coa = models.ForeignKey(
        ZohoChartOfAccount, on_delete=models.CASCADE, blank=True, null=True, related_name="igst_tally_expense_analysed_bills"
    )
    igst_debit_or_credit = models.CharField(
        choices=TRANSACTION_TYPE_CHOICES, max_length=10, blank=True, null=True, default="debit"
    )
    cgst = models.CharField(max_length=50, null=True, blank=True, default=0)
    cgst_coa = models.ForeignKey(
        ZohoChartOfAccount, on_delete=models.CASCADE, blank=True, null=True,
        related_name="cgst_tally_expense_analysed_bills"
    )
    cgst_debit_or_credit = models.CharField(
        choices=TRANSACTION_TYPE_CHOICES, max_length=10, blank=True, null=True, default="debit"
    )
    sgst = models.CharField(max_length=50, null=True, blank=True, default=0)
    sgst_coa = models.ForeignKey(
        ZohoChartOfAccount, on_delete=models.CASCADE, blank=True, null=True,
        related_name="sgst_tally_expense_analysed_bills"
    )
    sgst_debit_or_credit = models.CharField(
        choices=TRANSACTION_TYPE_CHOICES, max_length=10, blank=True, null=True, default="debit"
    )
    note = models.CharField(max_length=100, null=True, blank=True, default="Enter Your Description")

    # Line Items Consolidation Setting
    consolidate = models.BooleanField(
        default=False,
        help_text="If True, sync as single consolidated journal entry. If False, sync all individual entries."
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Analysed Journal Bill"
        verbose_name_plural = "Analysed Journal Bills"

    def __str__(self):
        return (
            self.selectBill.billmunshiName
            if self.selectBill and self.selectBill.billmunshiName
            else f"JournalZohoBill:{self.id}"
        )


class JournalZohoProduct(BaseTeamModel):
    """
    Represents a product line item for an expense bill.
    Contains details about the expense including the chart of accounts, amount,
    and whether it's a debit or credit entry.
    """
    TRANSACTION_TYPE_CHOICES = (
        ("credit", "Credit"),
        ("debit", "Debit"),
    )

    id = models.UUIDField(default=uuid.uuid4, unique=True, primary_key=True, editable=False)
    zohoBill = models.ForeignKey("JournalZohoBill", on_delete=models.CASCADE, related_name="products")
    item_details = models.CharField(max_length=2000, null=True, blank=True)
    chart_of_accounts = models.ForeignKey("ZohoChartOfAccount", on_delete=models.CASCADE, null=True, blank=True)
    amount = models.CharField(max_length=50, null=True, blank=True)
    debit_or_credit = models.CharField(choices=TRANSACTION_TYPE_CHOICES, max_length=10, null=True, blank=True,
                                       default="debit")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Journal Analysed Bill Product"
        verbose_name_plural = "Journal Analysed Bill Products"

    def __str__(self):
        return (
            self.zohoBill.selectBill.billmunshiName
            if self.zohoBill and self.zohoBill.selectBill and self.zohoBill.selectBill.billmunshiName
            else f"JournalZohoProduct:{self.id}"
        )


class JournalZohoConsolidatedProduct(BaseTeamModel):
    """
    Represents a consolidated product line item for journal bills.
    Used when consolidate=True in JournalZohoBill to avoid account conflicts.
    Contains aggregated data from multiple JournalZohoProduct entries.
    """
    TRANSACTION_TYPE_CHOICES = (
        ("credit", "Credit"),
        ("debit", "Debit"),
    )

    id = models.UUIDField(default=uuid.uuid4, unique=True, primary_key=True, editable=False)
    zohoBill = models.ForeignKey("JournalZohoBill", on_delete=models.CASCADE, related_name="consolidated_products")

    # Consolidated item details
    consolidated_item_details = models.TextField(
        null=True,
        blank=True,
        default="Multiple journal entries consolidated",
        help_text="Description for the consolidated journal entry"
    )

    # Financial data
    consolidated_amount = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Total consolidated amount"
    )

    # Account handling for consolidated entry
    chart_of_accounts = models.ForeignKey(
        "ZohoChartOfAccount",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        help_text="Primary chart of accounts for consolidated entry"
    )
    debit_or_credit = models.CharField(
        choices=TRANSACTION_TYPE_CHOICES,
        max_length=10,
        null=True,
        blank=True,
        default="debit",
        help_text="Transaction type for consolidated entry"
    )

    # Metadata
    original_entries_count = models.IntegerField(
        default=0,
        help_text="Number of original journal entries that were consolidated"
    )
    consolidation_notes = models.TextField(
        null=True,
        blank=True,
        help_text="Notes about how consolidation was performed"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Consolidated Journal Product"
        verbose_name_plural = "Consolidated Journal Products"

    def __str__(self):
        return f"Consolidated Journal: {self.consolidated_item_details[:50]} ({self.original_entries_count} entries)"


# ===============================
#         Expense Bills
# ===============================

class ExpenseBill(BaseTeamModel):
    """
    Represents an expense bill/invoice that has been uploaded to the system.
    Similar to VendorBill but specifically for expense transactions.
    Tracks the bill file, analysis status, and processing state.
    """
    BILL_STATUS_CHOICES = (
        ("Draft", "Draft"),
        ("Analysed", "Analysed"),
        ("Verified", "Verified"),
        ("Synced", "Synced"),
    )
    BILL_TYPE_CHOICES = (
        ("Single Invoice/File", "Single Invoice/File"),
        ("Multiple Invoice/File", "Multiple Invoice/File"),
    )

    id = models.UUIDField(default=uuid.uuid4, unique=True, primary_key=True, editable=False)
    billmunshiName = models.CharField(max_length=100, null=True, blank=True)
    file = models.FileField(upload_to=bill_upload_path, validators=[validate_file_extension, validate_bill_file_size])
    fileType = models.CharField(choices=BILL_TYPE_CHOICES, max_length=100, null=True, blank=True,
                                default="Single Invoice/File")
    analysed_data = models.JSONField(default=dict, null=True, blank=True)
    status = models.CharField(max_length=10, choices=BILL_STATUS_CHOICES, default="Draft", blank=True)
    process = models.BooleanField(default=False, help_text=("HAS-been-analysed flag: True once AI analysis has produced ``analysed_data`` and an ``analysed_headers`` row. Semantically means \"analysis done\", not \"currently processing\". For the in-progress state see ``is_processing``. Historical name — keep for backwards compat."))
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_expense_bills"
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

    # Bill ownership and description
    bill_belong_your_org = models.BooleanField(default=False, help_text="Whether this bill belongs to your organization")
    description = models.TextField(blank=True, null=True, help_text="Description of the bill")

    created_at = models.DateTimeField(auto_now_add=True)
    update_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Expense Bill"
        verbose_name_plural = "Expense Bills"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "billmunshiName"],
                name=UNIQUE_ZOHO_EXPENSE_BILL_NAME,
                # Partial: historical rows keep their (sometimes duplicated) names.
                condition=Q(created_at__gte=NAME_UNIQUENESS_ENFORCED_FROM),
            ),
        ]

    def __str__(self):
        return self.billmunshiName or f"ExpenseBill:{self.id}"

    def save(self, *args, **kwargs):
        return save_with_unique_name(
            self,
            super().save,
            name_field="billmunshiName",
            code="ZE",
            constraint=UNIQUE_ZOHO_EXPENSE_BILL_NAME,
            should_generate=not self.billmunshiName and bool(self.file),
            args=args,
            kwargs=kwargs,
        )


class ExpenseZohoBill(BaseTeamModel):
    """
    Represents an analyzed expense bill with extracted data ready for Zoho Books.
    Links to the original ExpenseBill and contains tax information and vendor details.
    """
    id = models.UUIDField(default=uuid.uuid4, unique=True, primary_key=True, editable=False)
    selectBill = models.ForeignKey("ExpenseBill", on_delete=models.CASCADE, null=True, blank=True)
    vendor = models.ForeignKey("ZohoVendor", on_delete=models.CASCADE, null=True, blank=True)
    chart_of_accounts = models.ForeignKey("ZohoChartOfAccount", on_delete=models.CASCADE, null=True, blank=True)
    bill_no = models.CharField(max_length=50, null=True, blank=True)
    bill_date = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    total = models.CharField(max_length=50, null=True, blank=True, default=0)
    igst = models.CharField(max_length=50, null=True, blank=True, default=0)
    cgst = models.CharField(max_length=50, null=True, blank=True, default=0)
    sgst = models.CharField(max_length=50, null=True, blank=True, default=0)
    note = models.CharField(max_length=100, null=True, blank=True, default="Enter Your Description")

    # Line Items Consolidation Setting
    consolidate = models.BooleanField(
        default=False,
        help_text="If True, sync as single consolidated expense entry. If False, sync all individual entries."
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Analysed Expense Bill"
        verbose_name_plural = "Analysed Expense Bills"

    def __str__(self):
        return (
            self.selectBill.billmunshiName
            if self.selectBill and self.selectBill.billmunshiName
            else f"JournalZohoBill:{self.id}"
        )


class ExpenseZohoProduct(BaseTeamModel):
    """
    Represents a product line item for an expense bill.
    Contains details about the expense including the chart of accounts, amount,
    and whether it's a debit or credit entry.
    """
    id = models.UUIDField(default=uuid.uuid4, unique=True, primary_key=True, editable=False)
    zohoBill = models.ForeignKey("ExpenseZohoBill", on_delete=models.CASCADE, related_name="products")
    item_details = models.CharField(max_length=2000, null=True, blank=True)
    amount = models.CharField(max_length=50, null=True, blank=True)
    chart_of_accounts = models.ForeignKey("ZohoChartOfAccount", on_delete=models.CASCADE, null=True, blank=True)
    taxes = models.ForeignKey("ZohoTaxes", on_delete=models.CASCADE, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Expense Analysed Bill Product"
        verbose_name_plural = "Expense Analysed Bill Products"

    def __str__(self):
        return (
            self.zohoBill.selectBill.billmunshiName
            if self.zohoBill and self.zohoBill.selectBill and self.zohoBill.selectBill.billmunshiName
            else f"ExpenseZohoProduct:{self.id}"
        )


class ExpenseZohoConsolidatedProduct(BaseTeamModel):
    """
    Represents a consolidated product line item for expense bills.
    Used when consolidate=True in ExpenseZohoBill to avoid account conflicts.
    Contains aggregated data from multiple ExpenseZohoProduct entries.
    """
    id = models.UUIDField(default=uuid.uuid4, unique=True, primary_key=True, editable=False)
    zohoBill = models.ForeignKey("ExpenseZohoBill", on_delete=models.CASCADE, related_name="consolidated_products")

    # Consolidated item details
    consolidated_item_details = models.TextField(
        null=True,
        blank=True,
        default="Multiple expense entries consolidated",
        help_text="Description for the consolidated expense entry"
    )

    # Financial data
    consolidated_amount = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Total consolidated amount"
    )

    # Account and tax handling for consolidated entry
    chart_of_accounts = models.ForeignKey(
        "ZohoChartOfAccount",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        help_text="Primary chart of accounts for consolidated entry"
    )
    taxes = models.ForeignKey(
        "ZohoTaxes",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        help_text="Tax applied to consolidated entry (highest tax rate or most common)"
    )

    # Metadata
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
        verbose_name = "Consolidated Expense Product"
        verbose_name_plural = "Consolidated Expense Products"

    def __str__(self):
        return f"Consolidated Expense: {self.consolidated_item_details[:50]} ({self.original_entries_count} entries)"



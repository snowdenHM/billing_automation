# apps/zoho/models.py

import os
import re
import uuid
from django.db import models
from django.db.models import Max
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.contrib.auth.models import User
from django.conf import settings

from apps.organizations.models import Organization


# -----------------------------
# Helpers / Base
# -----------------------------

def validate_file_extension(value):
    """
    Simple file extension validator for uploaded bills.
    Adjust the allowed list if needed.
    """
    allowed = {".pdf", ".png", ".jpg", ".jpeg"}
    ext = os.path.splitext(getattr(value, "name", ""))[1].lower()
    if ext not in allowed:
        raise ValidationError(f"Unsupported file type '{ext}'. Allowed: {', '.join(sorted(allowed))}")


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
            return False

        url = (
            "https://accounts.zoho.in/oauth/v2/token"
            f"?refresh_token={self.refreshToken}&client_id={self.clientId}"
            f"&client_secret={self.clientSecret}&grant_type=refresh_token"
        )

        try:
            import requests
            response = requests.post(url, timeout=30)

            if response.status_code == 200:
                data = response.json()
                if "access_token" in data:
                    self.accessToken = data["access_token"]
                    # Set expiry to 50 minutes from now (Zoho tokens last 1 hour)
                    self.token_expiry = timezone.now() + timezone.timedelta(minutes=50)
                    self.save(update_fields=["accessToken", "token_expiry", "update_at"])
                    return True
            # Log the error for debugging
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Failed to refresh Zoho token: {response.status_code} - {response.text}")
        except Exception as e:
            # Log the exception for debugging
            import logging
            logger = logging.getLogger(__name__)
            logger.exception(f"Exception refreshing Zoho token: {str(e)}")

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
    contactId = models.CharField(max_length=100, unique=True)
    companyName = models.CharField(max_length=100)
    gstNo = models.CharField(max_length=30)
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
    file = models.FileField(upload_to="bills/", validators=[validate_file_extension])
    fileType = models.CharField(choices=BILL_TYPE_CHOICES, max_length=100, null=True, blank=True, default="Single Invoice/File")
    analysed_data = models.JSONField(default=dict, null=True, blank=True)
    status = models.CharField(max_length=10, choices=BILL_STATUS_CHOICES, default="Draft", blank=True)
    process = models.BooleanField(default=False)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_vendor_bills"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    update_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Vendor Bill"
        verbose_name_plural = "Vendor Bills"

    def __str__(self):
        return self.billmunshiName or f"Bill:{self.id}"

    def save(self, *args, **kwargs):
        if not self.billmunshiName and self.file:
            # Extract the numeric part from the highest 'billmunshiName' starting with 'BM-ZV-'
            highest_bill_name = VendorBill.objects.filter(
                billmunshiName__startswith="BM-ZV-"
            ).aggregate(max_number=Max("billmunshiName"))["max_number"]
            if highest_bill_name:
                match = re.match(r"BM-ZV-(\d+)", highest_bill_name)
                if match:
                    existing_count = int(match.group(1)) + 1
                else:
                    existing_count = 1
            else:
                existing_count = 1

            self.billmunshiName = f"BM-ZV-{existing_count}"
        super().save(*args, **kwargs)


class VendorZohoBill(BaseTeamModel):
    """
    Represents an analyzed vendor bill with extracted data ready for Zoho Books.
    Links to the original VendorBill and contains tax information and vendor details.
    """
    TAX_TYPE_CHOICES = (
        ("TCS", "is_tcs_tax"),
        ("TDS", "is_tds_tax"),
    )

    id = models.UUIDField(default=uuid.uuid4, unique=True, primary_key=True, editable=False)
    selectBill = models.ForeignKey("VendorBill", on_delete=models.CASCADE, null=True, blank=True)
    vendor = models.ForeignKey("ZohoVendor", on_delete=models.CASCADE, null=True, blank=True)
    bill_no = models.CharField(max_length=50, null=True, blank=True)
    bill_date = models.DateField(null=True, blank=True)
    total = models.CharField(max_length=50, null=True, blank=True, default=0)
    igst = models.CharField(max_length=50, null=True, blank=True, default=0)
    cgst = models.CharField(max_length=50, null=True, blank=True, default=0)
    sgst = models.CharField(max_length=50, null=True, blank=True, default=0)
    tds_tcs_id = models.ForeignKey("ZohoTdsTcs", on_delete=models.CASCADE, null=True, blank=True)
    is_tax = models.CharField(choices=TAX_TYPE_CHOICES, max_length=100, null=True, blank=True, default="TDS")
    note = models.CharField(max_length=100, null=True, blank=True, default="Enter Your Description")
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
    item_name = models.CharField(max_length=100, null=True, blank=True)
    item_details = models.CharField(max_length=200, null=True, blank=True)
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
    rate = models.CharField(max_length=10, null=True, blank=True)
    quantity = models.CharField(max_length=10, null=True, blank=True, default=0)
    amount = models.CharField(max_length=10, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Analysed Bill Product"
        verbose_name_plural = "Analysed Bill Products"

    def __str__(self):
        return self.item_name or f"Product:{self.id}"


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
    file = models.FileField(upload_to="bills/", validators=[validate_file_extension])
    fileType = models.CharField(choices=BILL_TYPE_CHOICES, max_length=100, null=True, blank=True, default="Single Invoice/File")
    analysed_data = models.JSONField(default=dict, null=True, blank=True)
    status = models.CharField(max_length=10, choices=BILL_STATUS_CHOICES, default="Draft", blank=True)
    process = models.BooleanField(default=False)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_expense_bills"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    update_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Expense Bill"
        verbose_name_plural = "Expense Bills"

    def __str__(self):
        return self.billmunshiName or f"ExpenseBill:{self.id}"

    def save(self, *args, **kwargs):
        if not self.billmunshiName and self.file:
            # Extract the numeric part from the highest 'billmunshiName' starting with 'BM-ZE-'
            highest_bill_name = ExpenseBill.objects.filter(
                billmunshiName__startswith="BM-ZE-"
            ).aggregate(max_number=Max("billmunshiName"))["max_number"]
            if highest_bill_name:
                match = re.match(r"BM-ZE-(\d+)", highest_bill_name)
                if match:
                    existing_count = int(match.group(1)) + 1
                else:
                    existing_count = 1
            else:
                existing_count = 1

            self.billmunshiName = f"BM-ZE-{existing_count}"
        super().save(*args, **kwargs)


class ExpenseZohoBill(BaseTeamModel):
    """
    Represents an analyzed expense bill with extracted data ready for Zoho Books.
    Links to the original ExpenseBill and contains tax information and vendor details.
    """
    id = models.UUIDField(default=uuid.uuid4, unique=True, primary_key=True, editable=False)
    selectBill = models.ForeignKey("ExpenseBill", on_delete=models.CASCADE, null=True, blank=True)
    vendor = models.ForeignKey("ZohoVendor", on_delete=models.CASCADE, null=True, blank=True)
    bill_no = models.CharField(max_length=50, null=True, blank=True)
    bill_date = models.DateField(null=True, blank=True)
    total = models.CharField(max_length=50, null=True, blank=True, default=0)
    igst = models.CharField(max_length=50, null=True, blank=True, default=0)
    cgst = models.CharField(max_length=50, null=True, blank=True, default=0)
    sgst = models.CharField(max_length=50, null=True, blank=True, default=0)
    note = models.CharField(max_length=100, null=True, blank=True, default="Enter Your Description")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Analysed Expense Bill"
        verbose_name_plural = "Analysed Expense Bills"

    def __str__(self):
        return (
            self.selectBill.billmunshiName
            if self.selectBill and self.selectBill.billmunshiName
            else f"ExpenseZohoBill:{self.id}"
        )


class ExpenseZohoProduct(BaseTeamModel):
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
    zohoBill = models.ForeignKey("ExpenseZohoBill", on_delete=models.CASCADE, related_name="products")
    item_details = models.CharField(max_length=200, null=True, blank=True)
    chart_of_accounts = models.ForeignKey("ZohoChartOfAccount", on_delete=models.CASCADE, null=True, blank=True)
    vendor = models.ForeignKey("ZohoVendor", on_delete=models.CASCADE, null=True, blank=True)
    amount = models.CharField(max_length=10, null=True, blank=True)
    debit_or_credit = models.CharField(choices=TRANSACTION_TYPE_CHOICES, max_length=10, null=True, blank=True, default="credit")
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

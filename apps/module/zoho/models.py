# apps/zoho/models.py

import os
import re
import uuid
from django.db import models
from django.db.models import Max
from django.core.exceptions import ValidationError

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
    id = models.UUIDField(default=uuid.uuid4, unique=True, primary_key=True, editable=False)
    clientId = models.CharField(max_length=100)
    clientSecret = models.CharField(max_length=100)
    accessCode = models.CharField(max_length=200, default="Your Access Code")
    organisationId = models.CharField(max_length=100, default="Your organisationId")
    redirectUrl = models.CharField(max_length=200, default="Your Redirect URL")
    accessToken = models.CharField(max_length=200, null=True, blank=True)
    refreshToken = models.CharField(max_length=200, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    update_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Zoho Credentials"

    def __str__(self):
        return f"{self.organization.name} · ZohoCredentials"


# --------------
# Zoho Vendors
# --------------

class ZohoVendor(BaseTeamModel):
    id = models.UUIDField(default=uuid.uuid4, unique=True, primary_key=True, editable=False)
    contactId = models.CharField(max_length=100, unique=True)
    companyName = models.CharField(max_length=100)
    gstNo = models.CharField(max_length=30)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "Zoho Vendor"

    def __str__(self):
        return self.companyName


# -----------------------
# Zoho Chart of Accounts
# -----------------------

class ZohoChartOfAccount(BaseTeamModel):
    id = models.UUIDField(default=uuid.uuid4, unique=True, primary_key=True, editable=False)
    accountId = models.CharField(max_length=100)
    accountName = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "Zoho Chart Of Accounts"

    def __str__(self):
        return self.accountName


# -----------
# Zoho Taxes
# -----------

class ZohoTaxes(BaseTeamModel):
    id = models.UUIDField(default=uuid.uuid4, unique=True, primary_key=True, editable=False)
    taxId = models.CharField(max_length=100)
    taxName = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "Zoho Taxes"

    def __str__(self):
        return self.taxName


# -------------------
# Zoho TDS / TCS Tax
# -------------------

class Zoho_TDS_TCS(BaseTeamModel):
    taxChoice = (
        ("TCS", "tcs_tax"),
        ("TDS", "tds_tax"),
    )
    id = models.UUIDField(default=uuid.uuid4, unique=True, primary_key=True, editable=False)
    taxId = models.CharField(max_length=100)
    taxName = models.CharField(max_length=100)
    taxPercentage = models.CharField(max_length=100, null=True, blank=True, default=0)
    taxType = models.CharField(choices=taxChoice, max_length=100, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "Zoho TDS and TCS Taxes"

    def __str__(self):
        pct = self.taxPercentage if self.taxPercentage not in (None, "") else "0"
        return f"{self.taxName} ({pct}%)"


# ----------------
# Vendor Credits
# ----------------

class Zoho_Vendor_Credits(BaseTeamModel):
    id = models.UUIDField(default=uuid.uuid4, unique=True, primary_key=True, editable=False)
    vendor_id = models.CharField(max_length=100, null=True, blank=True)
    vendor_name = models.CharField(max_length=100, null=True, blank=True)
    vendor_credit_id = models.CharField(max_length=100, null=True, blank=True)
    vendor_credit_number = models.CharField(max_length=100, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "Zoho Vendor Credits"

    def __str__(self):
        return self.vendor_name or f"VendorCredit:{self.vendor_credit_id or self.id}"


# ===============================
#         Vendor Bills
# ===============================

class VendorBill(BaseTeamModel):
    billStatus = (
        ("Draft", "Draft"),
        ("Analysed", "Analysed"),
        ("Verified", "Verified"),
        ("Synced", "Synced"),
    )
    billType = (
        ("Single Invoice/File", "Single Invoice/File"),
        ("Multiple Invoice/File", "Multiple Invoice/File"),
    )

    id = models.UUIDField(default=uuid.uuid4, unique=True, primary_key=True, editable=False)
    billmunshiName = models.CharField(max_length=100, null=True, blank=True)
    file = models.FileField(upload_to="bills/", validators=[validate_file_extension])
    fileType = models.CharField(choices=billType, max_length=100, null=True, blank=True, default="Single Invoice/File")
    analysed_data = models.JSONField(default=dict, null=True, blank=True)
    status = models.CharField(max_length=10, choices=billStatus, default="Draft", blank=True)
    process = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    update_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Vendor Bill"

    def __str__(self):
        return self.billmunshiName or f"Bill:{self.id}"

    def save(self, *args, **kwargs):
        if not self.billmunshiName and self.file:
            # Extract the numeric part from the highest 'billmunshiName' starting with 'BM-ZV-'
            highest_bill_name = VendorBill.objects.filter(   # ← fix: use VendorBill
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
    taxChoice = (
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
    tds_tcs_id = models.ForeignKey("Zoho_TDS_TCS", on_delete=models.CASCADE, null=True, blank=True)
    is_tax = models.CharField(choices=taxChoice, max_length=100, null=True, blank=True, default="TDS")
    note = models.CharField(max_length=100, null=True, blank=True, default="Enter Your Description")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "Analysed Bill"

    def __str__(self):
        return self.bill_no or (self.selectBill.billmunshiName if self.selectBill else f"ZohoBill:{self.id}")


class VendorZohoProduct(BaseTeamModel):
    itc_eligibility_choices = (
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
        choices=itc_eligibility_choices,
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
        verbose_name_plural = "Analysed Bill Products"

    def __str__(self):
        return self.item_name or f"Product:{self.id}"


# ===============================
#         Expense Bills
# ===============================

class ExpenseBill(BaseTeamModel):
    billStatus = (
        ("Draft", "Draft"),
        ("Analysed", "Analysed"),
        ("Verified", "Verified"),
        ("Synced", "Synced"),
    )
    billType = (
        ("Single Invoice/File", "Single Invoice/File"),
        ("Multiple Invoice/File", "Multiple Invoice/File"),
    )

    id = models.UUIDField(default=uuid.uuid4, unique=True, primary_key=True, editable=False)
    billmunshiName = models.CharField(max_length=100, null=True, blank=True)
    file = models.FileField(upload_to="bills/", validators=[validate_file_extension])
    fileType = models.CharField(choices=billType, max_length=100, null=True, blank=True, default="Single Invoice/File")
    analysed_data = models.JSONField(default=dict, null=True, blank=True)
    status = models.CharField(max_length=10, choices=billStatus, default="Draft", blank=True)
    process = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    update_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Expense Bill"

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
        verbose_name_plural = "Expense Analysed Bill"

    def __str__(self):
        return (
            self.selectBill.billmunshiName
            if self.selectBill and self.selectBill.billmunshiName
            else f"ExpenseZohoBill:{self.id}"
        )


class ExpenseZohoProduct(BaseTeamModel):
    expense_choice = (
        ("credit", "credit"),
        ("debit", "debit"),
    )

    id = models.UUIDField(default=uuid.uuid4, unique=True, primary_key=True, editable=False)
    zohoBill = models.ForeignKey("ExpenseZohoBill", on_delete=models.CASCADE, related_name="products")
    item_details = models.CharField(max_length=200, null=True, blank=True)
    chart_of_accounts = models.ForeignKey("ZohoChartOfAccount", on_delete=models.CASCADE, null=True, blank=True)
    vendor = models.ForeignKey("ZohoVendor", on_delete=models.CASCADE, null=True, blank=True)
    amount = models.CharField(max_length=10, null=True, blank=True)
    debit_or_credit = models.CharField(choices=expense_choice, max_length=10, null=True, blank=True, default="credit")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "Expense Analysed Bill Products"

    def __str__(self):
        return (
            self.zohoBill.selectBill.billmunshiName
            if self.zohoBill and self.zohoBill.selectBill and self.zohoBill.selectBill.billmunshiName
            else f"ExpenseZohoProduct:{self.id}"
        )

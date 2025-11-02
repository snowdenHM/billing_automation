# apps/zoho/admin.py

from django.contrib import admin
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.urls import reverse

from .models import (
    ZohoCredentials,
    ZohoVendor,
    ZohoChartOfAccount,
    ZohoTaxes,
    ZohoTdsTcs,
    VendorBill,
    VendorZohoBill,
    VendorZohoProduct,
    JournalBill,
    JournalZohoBill,
    JournalZohoProduct,
    ExpenseBill,
    ExpenseZohoBill,
    ExpenseZohoProduct,
)

# -----------------------------
# Base admin helpers
# -----------------------------

class BaseOrgScopedAdmin(admin.ModelAdmin):
    """
    - If user isn't superuser and has organization_id, scope queryset to it.
    - Auto-fill organization on save when missing.
    - Make created_at / update_at readonly when present on the model.
    """

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if hasattr(self.model, "organization"):
            qs = qs.select_related("organization")
        if request.user.is_superuser:
            return qs

        user_org_id = getattr(request.user, "organization_id", None)
        if user_org_id and hasattr(self.model, "organization_id"):
            qs = qs.filter(organization_id=user_org_id)
        return qs

    def save_model(self, request, obj, form, change):
        if hasattr(obj, "organization_id") and not obj.organization_id:
            user_org_id = getattr(request.user, "organization_id", None)
            if user_org_id:
                obj.organization_id = user_org_id
        super().save_model(request, obj, form, change)

    def get_readonly_fields(self, request, obj=None):
        ro = list(super().get_readonly_fields(request, obj))
        # Add common timestamp fields only if they exist on the model
        model_field_names = {f.name for f in self.model._meta.get_fields()}
        for f in ("created_at", "update_at"):
            if f in model_field_names and f not in ro:
                ro.append(f)
        return ro


def admin_change_url_for_instance(obj):
    """Return the admin change URL for any model instance."""
    return reverse(f"admin:{obj._meta.app_label}_{obj._meta.model_name}_change", args=[obj.pk])


# -----------------------------
# Credentials / Master data
# -----------------------------

@admin.register(ZohoCredentials)
class ZohoCredentialsAdmin(BaseOrgScopedAdmin):
    list_display = ["organization", "clientId", "token_expiry", "created_at"]
    list_filter = ["created_at", "token_expiry"]
    search_fields = ["organization__name", "clientId"]
    readonly_fields = ["accessToken", "refreshToken", "token_expiry"]


@admin.register(ZohoVendor)
class ZohoVendorAdmin(BaseOrgScopedAdmin):
    list_display = ["companyName", "gstNo", "contactId", "organization"]
    list_filter = ["organization"]
    search_fields = ["companyName", "gstNo", "contactId"]


@admin.register(ZohoChartOfAccount)
class ZohoChartOfAccountAdmin(BaseOrgScopedAdmin):
    list_display = ["accountName", "accountId", "organization"]
    list_filter = ["organization"]
    search_fields = ["accountName", "accountId"]


@admin.register(ZohoTaxes)
class ZohoTaxesAdmin(BaseOrgScopedAdmin):
    list_display = ["taxName", "taxId", "organization"]
    list_filter = ["organization"]
    search_fields = ["taxName", "taxId"]


@admin.register(ZohoTdsTcs)
class ZohoTDSTCSAdmin(BaseOrgScopedAdmin):
    list_display = ["taxName", "taxId", "organization"]
    list_filter = ["organization"]
    search_fields = ["taxName", "taxId"]


# @admin.register(ZohoVendorCredit)
# class ZohoVendorCreditsAdmin(BaseOrgScopedAdmin):
#     list_display = ("organization", "vendor_name", "vendor_credit_number", "vendor_credit_id", "created_at")
#     search_fields = ("vendor_name", "vendor_credit_number", "vendor_credit_id", "organization__name")
#     list_filter = ("organization",)
#     readonly_fields = ("created_at",)
#     autocomplete_fields = ("organization",)


# -----------------------------
# Journal Bills & Products
# -----------------------------

class JournalZohoProductInline(admin.TabularInline):
    model = JournalZohoProduct
    extra = 0
    autocomplete_fields = ("chart_of_accounts",)
    fields = (
        "item_details",
        "chart_of_accounts",
        "amount",
        "debit_or_credit",
        "created_at",
    )
    readonly_fields = ("created_at",)


@admin.register(JournalBill)
class JournalBillAdmin(BaseOrgScopedAdmin):
    list_display = ["billmunshiName", "fileType", "status", "process", "uploaded_by", "organization", "created_at"]
    list_filter = ["status", "fileType", "process", "uploaded_by", "organization", "created_at"]
    search_fields = ["billmunshiName", "uploaded_by__username", "uploaded_by__first_name", "uploaded_by__last_name"]
    readonly_fields = ["analysed_data", "created_at", "update_at"]
    fields = (
        "organization",
        "billmunshiName",
        "file",
        "fileType",
        "analysed_data",
        "status",
        "process",
        "uploaded_by",
        "created_at",
        "update_at",
    )
    autocomplete_fields = ("organization", "uploaded_by")

    @admin.display(description="File", ordering="file")
    def file_link(self, obj):
        if obj.file:
            return format_html('<a href="{}" target="_blank">Open</a>', obj.file.url)
        return "-"


@admin.register(JournalZohoBill)
class JournalZohoBillAdmin(BaseOrgScopedAdmin):
    list_display = [
        "bill_no",
        "vendor",
        "bill_date",
        "total",
        "organization",
    ]
    list_filter = ["bill_date", "organization", "vendor"]
    search_fields = ["bill_no", "vendor__companyName"]
    readonly_fields = ("created_at",)
    inlines = [JournalZohoProductInline]
    autocomplete_fields = ("organization", "vendor", "selectBill")

    @admin.display(description="Selected Bill")
    def selectBill_link(self, obj):
        if obj.selectBill_id:
            url = admin_change_url_for_instance(obj.selectBill)
            label = obj.selectBill.billmunshiName or str(obj.selectBill_id)
            return mark_safe(f'<a href="{url}">{label}</a>')
        return "-"


# -----------------------------
# Vendor Bills & Products
# -----------------------------

class VendorZohoProductInline(admin.TabularInline):
    model = VendorZohoProduct
    extra = 0
    autocomplete_fields = ("chart_of_accounts", "taxes")
    fields = (
        "item_name",
        "item_details",
        "chart_of_accounts",
        "taxes",
        "reverse_charge_tax_id",
        "itc_eligibility",
        "rate",
        "quantity",
        "amount",
        "created_at",
    )
    readonly_fields = ("created_at",)


@admin.register(VendorBill)
class VendorBillAdmin(BaseOrgScopedAdmin):
    list_display = ["billmunshiName", "fileType", "status", "process", "uploaded_by", "organization", "created_at"]
    list_filter = ["status", "fileType", "process", "uploaded_by", "organization", "created_at"]
    search_fields = ["billmunshiName", "uploaded_by__username", "uploaded_by__first_name", "uploaded_by__last_name"]
    readonly_fields = ["analysed_data", "created_at", "update_at"]
    fields = (
        "organization",
        "billmunshiName",
        "file",
        "fileType",
        "analysed_data",
        "status",
        "process",
        "uploaded_by",
        "created_at",
        "update_at",
    )
    autocomplete_fields = ("organization", "uploaded_by")

    @admin.display(description="File", ordering="file")
    def file_link(self, obj):
        if obj.file:
            return format_html('<a href="{}" target="_blank">Open</a>', obj.file.url)
        return "-"


@admin.register(VendorZohoBill)
class VendorZohoBillAdmin(BaseOrgScopedAdmin):
    list_display = [
        "bill_no",
        "vendor",
        "bill_date",
        "due_date",
        "total",
        "discount_amount",
        "organization",
    ]
    list_filter = ["bill_date", "due_date", "organization", "vendor", "discount_type"]
    search_fields = ["bill_no", "vendor__companyName"]
    readonly_fields = ("created_at",)
    inlines = [VendorZohoProductInline]
    autocomplete_fields = ("organization", "vendor", "selectBill", "tds_tcs_id", "discount_account")
    
    fieldsets = (
        ('Bill Information', {
            'fields': ('selectBill', 'vendor', 'bill_no', 'bill_date', 'due_date', 'note')
        }),
        ('Financial Details', {
            'fields': ('total', 'discount_type', 'discount', 'discount_amount', 'discount_account', 'adjustment_amount', 'adjustment_description')
        }),
        ('Tax Information', {
            'fields': ('igst', 'cgst', 'sgst', 'tds_tcs_id', 'is_tax')
        }),
        ('Metadata', {
            'fields': ('organization', 'created_at')
        }),
    )

    @admin.display(description="Selected Bill")
    def selectBill_link(self, obj):
        if obj.selectBill_id:
            url = admin_change_url_for_instance(obj.selectBill)
            label = obj.selectBill.billmunshiName or str(obj.selectBill_id)
            return mark_safe(f'<a href="{url}">{label}</a>')
        return "-"


# -----------------------------
# Expense Bills & Products
# -----------------------------

class ExpenseZohoProductInline(admin.TabularInline):
    model = ExpenseZohoProduct
    extra = 0
    fields = (
        "item_details",
        "amount",
        "chart_of_accounts",
        "taxes",
        "created_at",
    )
    readonly_fields = ("created_at",)


@admin.register(ExpenseBill)
class ExpenseBillAdmin(BaseOrgScopedAdmin):
    list_display = ["billmunshiName", "status", "fileType", "uploaded_by", "organization", "created_at"]
    list_filter = ["status", "fileType", "uploaded_by", "organization", "created_at"]
    search_fields = ["billmunshiName", "uploaded_by__username", "uploaded_by__first_name", "uploaded_by__last_name"]
    readonly_fields = ["billmunshiName", "analysed_data", "created_at", "update_at"]
    fields = (
        "organization",
        "billmunshiName",
        "file",
        "fileType",
        "analysed_data",
        "status",
        "process",
        "uploaded_by",
        "created_at",
        "update_at",
    )
    autocomplete_fields = ("organization", "uploaded_by")

    @admin.display(description="File", ordering="file")
    def file_link(self, obj):
        if obj.file:
            return format_html('<a href="{}" target="_blank">Open</a>', obj.file.url)
        return "-"


@admin.register(ExpenseZohoBill)
class ExpenseZohoBillAdmin(BaseOrgScopedAdmin):
    list_display = [
        "id",
        "selectBill_link",
        "vendor",
        "bill_no",
        "bill_date",
        "total",
        "organization",
    ]
    list_filter = ["bill_date", "organization", "created_at"]
    search_fields = ["bill_no", "vendor__companyName", "selectBill__billmunshiName"]
    readonly_fields = ("created_at",)
    inlines = [ExpenseZohoProductInline]
    autocomplete_fields = ("organization", "selectBill", "vendor")

    @admin.display(description="Expense Bill")
    def selectBill_link(self, obj):
        if obj.selectBill_id:
            url = admin_change_url_for_instance(obj.selectBill)
            label = obj.selectBill.billmunshiName or str(obj.selectBill_id)
            return mark_safe(f'<a href="{url}">{label}</a>')
        return "-"


@admin.register(ExpenseZohoProduct)
class ExpenseZohoProductAdmin(BaseOrgScopedAdmin):
    list_display = (
        "organization",
        "zohoBill_link",
        "item_details",
        "amount",
        "created_at",
    )
    search_fields = (
        "item_details",
        "zohoBill__bill_no",
        "zohoBill__selectBill__billmunshiName",
        "organization__name",
    )
    list_filter = ("organization", "created_at")
    readonly_fields = ("created_at",)
    autocomplete_fields = ("organization", "zohoBill", "chart_of_accounts", "taxes")

    @admin.display(description="Zoho Bill")
    def zohoBill_link(self, obj):
        if obj.zohoBill_id:
            url = admin_change_url_for_instance(obj.zohoBill)
            label = obj.zohoBill.bill_no or str(obj.zohoBill_id)
            return mark_safe(f'<a href="{url}">{label}</a>')
        return "-"

# apps/zoho/admin.py

from django.contrib import admin
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from apps.common.admin import BaseOrgAdmin, FKLinkMixin, admin_change_url

from .models import (
    ZohoCredentials,
    ZohoVendor,
    ZohoChartOfAccount,
    ZohoTaxes,
    ZohoTdsTcs,
    VendorBill,
    VendorZohoBill,
    VendorZohoProduct,
    VendorZohoConsolidatedProduct,
    JournalBill,
    JournalZohoBill,
    JournalZohoProduct,
    ExpenseBill,
    ExpenseZohoBill,
    ExpenseZohoProduct,
)

# Keep backward compat alias in case anything imports from here
BaseOrgScopedAdmin = BaseOrgAdmin


# -----------------------------
# Credentials / Master data
# -----------------------------

@admin.register(ZohoCredentials)
class ZohoCredentialsAdmin(BaseOrgAdmin):
    """Admin interface for Zoho API Credentials."""
    
    list_display = ["organization", "clientId", "token_expiry", "created_at"]
    list_filter = ["created_at", "token_expiry"]
    search_fields = ["organization__name", "clientId"]
    readonly_fields = ["created_at", "update_at"]
    date_hierarchy = "created_at"
    list_per_page = 50
    autocomplete_fields = ["organization"]
    
    fieldsets = (
        ('Organization', {
            'fields': ('organization',)
        }),
        ('Credentials', {
            'fields': ('clientId', 'clientSecret', 'token_expiry')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'update_at'),
            'classes': ('collapse',)
        }),
    )



@admin.register(ZohoVendor)
class ZohoVendorAdmin(BaseOrgAdmin):
    """Admin interface for Zoho Vendors."""
    
    list_display = ["companyName", "gstNo", "contactId", "organization", "created_at"]
    list_filter = ["organization", "created_at"]
    search_fields = ["companyName", "gstNo", "contactId"]
    readonly_fields = ["created_at"]
    date_hierarchy = "created_at"
    list_per_page = 50
    autocomplete_fields = ["organization"]


@admin.register(ZohoChartOfAccount)
class ZohoChartOfAccountAdmin(BaseOrgAdmin):
    """Admin interface for Zoho Chart of Accounts."""
    
    list_display = ["accountName", "accountId", "organization", "created_at"]
    list_filter = ["organization", "created_at"]
    search_fields = ["accountName", "accountId"]
    readonly_fields = ["created_at"]
    date_hierarchy = "created_at"
    list_per_page = 50
    autocomplete_fields = ["organization"]


@admin.register(ZohoTaxes)
class ZohoTaxesAdmin(BaseOrgAdmin):
    """Admin interface for Zoho Taxes."""
    
    list_display = ["taxName", "taxId", "organization", "created_at"]
    list_filter = ["organization", "created_at"]
    search_fields = ["taxName", "taxId"]
    readonly_fields = ["created_at"]
    date_hierarchy = "created_at"
    list_per_page = 50
    autocomplete_fields = ["organization"]


@admin.register(ZohoTdsTcs)
class ZohoTDSTCSAdmin(BaseOrgAdmin):
    """Admin interface for Zoho TDS/TCS."""
    
    list_display = ["taxName", "taxId", "organization", "created_at"]
    list_filter = ["organization", "created_at"]
    search_fields = ["taxName", "taxId"]
    readonly_fields = ["created_at"]
    date_hierarchy = "created_at"
    list_per_page = 50
    autocomplete_fields = ["organization"]


# @admin.register(ZohoVendorCredit)
# class ZohoVendorCreditsAdmin(BaseOrgAdmin):
#     list_display = ("organization", "vendor_name", "vendor_credit_number", "vendor_credit_id", "created_at")
#     search_fields = ("vendor_name", "vendor_credit_number", "vendor_credit_id", "organization__name")
#     list_filter = ("organization",)
#     readonly_fields = ("created_at",)
#     autocomplete_fields = ("organization",)


# -----------------------------
# Journal Bills & Products
# -----------------------------

class JournalZohoProductInline(admin.TabularInline):
    """Inline admin for Journal Zoho Products."""
    
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
class JournalBillAdmin(BaseOrgAdmin):
    """Admin interface for Journal Bills."""
    
    list_display = ["billmunshiName", "fileType", "status", "process", "uploaded_by", "organization", "created_at"]
    list_filter = ["status", "fileType", "process", "uploaded_by", "organization", "created_at"]
    search_fields = ["billmunshiName", "uploaded_by__username", "uploaded_by__first_name", "uploaded_by__last_name"]
    readonly_fields = ["analysed_data", "created_at", "update_at"]
    date_hierarchy = "created_at"
    list_per_page = 50
    autocomplete_fields = ("organization", "uploaded_by")
    
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

    @admin.display(description="File", ordering="file")
    def file_link(self, obj):
        if obj.file:
            return format_html('<a href="{}" target="_blank">Open</a>', obj.file.url)
        return "-"


@admin.register(JournalZohoBill)
class JournalZohoBillAdmin(BaseOrgAdmin):
    """Admin interface for Journal Zoho Bills."""
    
    list_display = [
        "bill_no",
        "vendor",
        "bill_date",
        "total",
        "organization",
    ]
    list_filter = ["bill_date", "organization", "vendor", "created_at"]
    search_fields = ["bill_no", "vendor__companyName"]
    readonly_fields = ("created_at",)
    date_hierarchy = "bill_date"
    list_per_page = 50
    inlines = [JournalZohoProductInline]
    autocomplete_fields = ("organization", "vendor", "selectBill")

    @admin.display(description="Selected Bill")
    def selectBill_link(self, obj):
        if obj.selectBill_id:
            url = admin_change_url(obj.selectBill)
            label = obj.selectBill.billmunshiName or str(obj.selectBill_id)
            return mark_safe(f'<a href="{url}">{label}</a>')
        return "-"


# -----------------------------
# Vendor Bills & Products
# -----------------------------

class VendorZohoProductInline(admin.TabularInline):
    """Inline admin for Vendor Zoho Products."""
    
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
class VendorBillAdmin(BaseOrgAdmin):
    """Admin interface for Vendor Bills."""
    
    list_display = [
        "billmunshiName", "status", "fileType", "process", "is_duplicate", "bill_belong_your_org", 
        "is_processing", "uploaded_by", "organization", "created_at"
    ]
    list_filter = [
        "status", "fileType", "process", "is_duplicate", "bill_belong_your_org", "is_processing",
        "uploaded_by", "organization", "created_at"
    ]
    search_fields = ["billmunshiName", "uploaded_by__username", "uploaded_by__first_name", "uploaded_by__last_name"]
    readonly_fields = ["analysed_data", "created_at", "update_at", "duplicate_matched_bills", "processing_error"]
    date_hierarchy = "created_at"
    list_per_page = 50
    autocomplete_fields = ("organization", "uploaded_by")
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('organization', 'billmunshiName', 'file', 'fileType', 'analysed_data')
        }),
        ('Status & Processing', {
            'fields': ('status', 'process', 'is_processing', 'processing_error', 'job_id')
        }),
        ('Duplicate Detection', {
            'fields': ('is_duplicate', 'duplicate_description', 'duplicate_score', 'duplicate_matched_bills')
        }),
        ('External Bill Validation', {
            'fields': ('bill_belong_your_org', 'description')
        }),
        ('Metadata', {
            'fields': ('uploaded_by', 'created_at', 'update_at'),
            'classes': ('collapse',)
        }),
    )
    
    def get_queryset(self, request):
        """Optimize queryset with select_related."""
        qs = super().get_queryset(request)
        return qs.select_related('organization', 'uploaded_by')
    
    @admin.display(description="Duplicate", boolean=True)
    def duplicate_status(self, obj):
        return obj.is_duplicate
    
    @admin.display(description="External", boolean=True)
    def external_status(self, obj):
        return not obj.bill_belong_your_org if obj.bill_belong_your_org is not None else None
    
    @admin.display(description="Processing", boolean=True)
    def processing_status(self, obj):
        return obj.is_processing

    @admin.display(description="File", ordering="file")
    def file_link(self, obj):
        if obj.file:
            return format_html('<a href="{}" target="_blank">Open</a>', obj.file.url)
        return "-"


@admin.register(VendorZohoBill)
class VendorZohoBillAdmin(BaseOrgAdmin):
    """Admin interface for Vendor Zoho Bills."""
    
    list_display = [
        "bill_no",
        "vendor",
        "bill_date",
        "due_date",
        "total",
        "discount_amount",
        "organization",
    ]
    list_filter = ["bill_date", "due_date", "organization", "vendor", "discount_type", "created_at"]
    search_fields = ["bill_no", "vendor__companyName"]
    readonly_fields = ("created_at",)
    date_hierarchy = "bill_date"
    list_per_page = 50
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
            'fields': ('organization', 'created_at'),
            'classes': ('collapse',)
        }),
    )
    
    def get_queryset(self, request):
        """Optimize queryset with select_related."""
        qs = super().get_queryset(request)
        return qs.select_related('organization', 'vendor', 'selectBill')

    @admin.display(description="Selected Bill")
    def selectBill_link(self, obj):
        if obj.selectBill_id:
            url = admin_change_url(obj.selectBill)
            label = obj.selectBill.billmunshiName or str(obj.selectBill_id)
            return mark_safe(f'<a href="{url}">{label}</a>')
        return "-"


# -----------------------------
# Expense Bills & Products
# -----------------------------

class ExpenseZohoProductInline(admin.TabularInline):
    """Inline admin for Expense Zoho Products."""
    
    model = ExpenseZohoProduct
    extra = 0
    autocomplete_fields = ("chart_of_accounts", "taxes")
    fields = (
        "item_details",
        "amount",
        "chart_of_accounts",
        "taxes",
        "created_at",
    )
    readonly_fields = ("created_at",)


@admin.register(ExpenseBill)
class ExpenseBillAdmin(BaseOrgAdmin):
    """Admin interface for Expense Bills."""
    
    list_display = ["billmunshiName", "status", "fileType", "uploaded_by", "organization", "created_at"]
    list_filter = ["status", "fileType", "uploaded_by", "organization", "created_at"]
    search_fields = ["billmunshiName", "uploaded_by__username", "uploaded_by__first_name", "uploaded_by__last_name"]
    readonly_fields = ["billmunshiName", "analysed_data", "created_at", "update_at"]
    date_hierarchy = "created_at"
    list_per_page = 50
    autocomplete_fields = ("organization", "uploaded_by")
    
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
    
    def get_queryset(self, request):
        """Optimize queryset with select_related."""
        qs = super().get_queryset(request)
        return qs.select_related('organization', 'uploaded_by')

    @admin.display(description="File", ordering="file")
    def file_link(self, obj):
        if obj.file:
            return format_html('<a href="{}" target="_blank">Open</a>', obj.file.url)
        return "-"


@admin.register(ExpenseZohoBill)
class ExpenseZohoBillAdmin(BaseOrgAdmin):
    """Admin interface for Expense Zoho Bills."""
    
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
    date_hierarchy = "bill_date"
    list_per_page = 50
    inlines = [ExpenseZohoProductInline]
    autocomplete_fields = ("organization", "selectBill", "vendor")
    
    def get_queryset(self, request):
        """Optimize queryset with select_related."""
        qs = super().get_queryset(request)
        return qs.select_related('organization', 'vendor', 'selectBill')

    @admin.display(description="Expense Bill")
    def selectBill_link(self, obj):
        if obj.selectBill_id:
            url = admin_change_url(obj.selectBill)
            label = obj.selectBill.billmunshiName or str(obj.selectBill_id)
            return mark_safe(f'<a href="{url}">{label}</a>')
        return "-"


@admin.register(ExpenseZohoProduct)
class ExpenseZohoProductAdmin(BaseOrgAdmin):
    """Admin interface for Expense Zoho Products."""
    
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
    date_hierarchy = "created_at"
    list_per_page = 50
    autocomplete_fields = ("organization", "zohoBill", "chart_of_accounts", "taxes")
    
    def get_queryset(self, request):
        """Optimize queryset with select_related."""
        qs = super().get_queryset(request)
        return qs.select_related('organization', 'zohoBill')

    @admin.display(description="Zoho Bill")
    def zohoBill_link(self, obj):
        if obj.zohoBill_id:
            url = admin_change_url(obj.zohoBill)
            label = obj.zohoBill.bill_no or str(obj.zohoBill_id)
            return mark_safe(f'<a href="{url}">{label}</a>')
        return "-"


# -----------------------------
# Consolidated Products
# -----------------------------

@admin.register(VendorZohoConsolidatedProduct)
class VendorZohoConsolidatedProductAdmin(BaseOrgAdmin):
    """Admin interface for Vendor Zoho Consolidated Products."""
    
    list_display = (
        "organization",
        "zohoBill_link",
        "consolidated_item_name",
        "original_items_count",
        "consolidated_amount",
        "created_at",
    )
    search_fields = (
        "consolidated_item_name",
        "zohoBill__bill_no",
        "zohoBill__selectBill__billmunshiName",
        "organization__name",
    )
    list_filter = ("organization", "created_at")
    readonly_fields = ("created_at", "updated_at")
    date_hierarchy = "created_at"
    list_per_page = 50
    autocomplete_fields = ("organization", "zohoBill", "chart_of_accounts", "taxes")

    fieldsets = (
        ('Consolidated Item Details', {
            'fields': ('consolidated_item_name', 'consolidated_item_details', 'original_items_count')
        }),
        ('Financial Information', {
            'fields': ('total_quantity', 'consolidated_rate', 'consolidated_amount')
        }),
        ('Tax and Account Settings', {
            'fields': ('chart_of_accounts', 'taxes', 'itc_eligibility', 'reverse_charge_tax_id')
        }),
        ('Metadata', {
            'fields': ('zohoBill', 'organization', 'consolidation_notes', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def get_queryset(self, request):
        """Optimize queryset with select_related."""
        qs = super().get_queryset(request)
        return qs.select_related('organization', 'zohoBill', 'zohoBill__selectBill')

    @admin.display(description="Zoho Bill")
    def zohoBill_link(self, obj):
        if obj.zohoBill_id:
            url = admin_change_url(obj.zohoBill)
            label = obj.zohoBill.bill_no or str(obj.zohoBill_id)
            return mark_safe(f'<a href="{url}">{label}</a>')
        return "-"



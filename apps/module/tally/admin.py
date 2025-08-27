from django.contrib import admin
from django.utils.html import format_html

from .models import (
    ParentLedger,
    Ledger,
    TallyConfig,
    TallyVendorBill,
    TallyVendorAnalyzedBill,
    TallyVendorAnalyzedProduct,
    TallyExpenseBill,
    TallyExpenseAnalyzedBill,
    TallyExpenseAnalyzedProduct,
)


class BaseOrgAdmin(admin.ModelAdmin):
    """
    Base admin class for organization-scoped models.
    - Shows organization in list display
    - Filters by organization
    - Makes created_at/updated_at readonly
    """
    list_display = ('id', 'organization')
    list_filter = ('organization',)

    def get_readonly_fields(self, request, obj=None):
        ro = list(super().get_readonly_fields(request, obj))
        # Add common timestamp fields if they exist on the model
        model_field_names = {f.name for f in self.model._meta.get_fields()}
        for f in ("created_at", "updated_at", "update_at"):
            if f in model_field_names and f not in ro:
                ro.append(f)
        return ro


@admin.register(ParentLedger)
class ParentLedgerAdmin(BaseOrgAdmin):
    list_display = ('id', 'parent', 'organization', 'created_at')
    search_fields = ('parent',)
    readonly_fields = ('created_at', 'update_at')


@admin.register(Ledger)
class LedgerAdmin(BaseOrgAdmin):
    list_display = ('id', 'name', 'parent', 'organization', 'gst_in')
    search_fields = ('name', 'master_id', 'alter_id', 'gst_in')
    list_filter = ('organization', 'parent')
    autocomplete_fields = ('parent',)


class TallyConfigParentInline(admin.TabularInline):
    model = TallyConfig.igst_parents.through
    extra = 1
    verbose_name = "IGST Parent"
    verbose_name_plural = "IGST Parents"


@admin.register(TallyConfig)
class TallyConfigAdmin(BaseOrgAdmin):
    list_display = ('id', 'organization')
    filter_horizontal = (
        'igst_parents', 'cgst_parents', 'sgst_parents',
        'vendor_parents', 'chart_of_accounts_parents',
        'chart_of_accounts_expense_parents'
    )


@admin.register(TallyVendorBill)
class TallyVendorBillAdmin(BaseOrgAdmin):
    list_display = ('id', 'billmunshiName', 'file_link', 'fileType', 'status', 'process', 'organization', 'created_at')
    list_filter = ('organization', 'status', 'fileType', 'process')
    search_fields = ('billmunshiName',)
    readonly_fields = ('created_at', 'updated_at')

    @admin.display(description='File')
    def file_link(self, obj):
        if obj.file:
            return format_html('<a href="{}" target="_blank">View</a>', obj.file.url)
        return '-'


class TallyVendorAnalyzedProductInline(admin.TabularInline):
    model = TallyVendorAnalyzedProduct
    extra = 0
    fields = ('item_name', 'item_details', 'taxes', 'price', 'quantity', 'amount', 'product_gst')
    readonly_fields = ('created_at',)


@admin.register(TallyVendorAnalyzedBill)
class TallyVendorAnalyzedBillAdmin(BaseOrgAdmin):
    list_display = ('id', 'bill_ref', 'vendor', 'bill_no', 'bill_date', 'total', 'organization', 'created_at')
    list_filter = ('organization', 'selectBill__status', 'gst_type')
    search_fields = ('bill_no', 'selectBill__billmunshiName', 'vendor__name')
    readonly_fields = ('created_at',)
    inlines = [TallyVendorAnalyzedProductInline]
    autocomplete_fields = ('vendor', 'igst_taxes', 'cgst_taxes', 'sgst_taxes')

    @admin.display(description='Bill')
    def bill_ref(self, obj):
        if obj.selectBill:
            return obj.selectBill.billmunshiName
        return f"ID: {obj.id}"


@admin.register(TallyExpenseBill)
class TallyExpenseBillAdmin(BaseOrgAdmin):
    list_display = ('id', 'billmunshiName', 'file_link', 'fileType', 'status', 'process', 'organization', 'created_at')
    list_filter = ('organization', 'status', 'fileType', 'process')
    search_fields = ('billmunshiName',)
    readonly_fields = ('created_at', 'updated_at')

    @admin.display(description='File')
    def file_link(self, obj):
        if obj.file:
            return format_html('<a href="{}" target="_blank">View</a>', obj.file.url)
        return '-'


class TallyExpenseAnalyzedProductInline(admin.TabularInline):
    model = TallyExpenseAnalyzedProduct
    extra = 0
    fields = ('item_details', 'chart_of_accounts', 'amount', 'debit_or_credit')
    readonly_fields = ('created_at',)


@admin.register(TallyExpenseAnalyzedBill)
class TallyExpenseAnalyzedBillAdmin(BaseOrgAdmin):
    list_display = ('id', 'bill_ref', 'vendor', 'bill_no', 'bill_date', 'total', 'organization', 'created_at')
    list_filter = ('organization', 'selectBill__status')
    search_fields = ('bill_no', 'voucher', 'selectBill__billmunshiName', 'vendor__name')
    readonly_fields = ('created_at',)
    inlines = [TallyExpenseAnalyzedProductInline]
    autocomplete_fields = ('vendor', 'igst_taxes', 'cgst_taxes', 'sgst_taxes')

    @admin.display(description='Bill')
    def bill_ref(self, obj):
        if obj.selectBill:
            return obj.selectBill.billmunshiName
        return f"ID: {obj.id}"

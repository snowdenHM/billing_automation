from django.contrib import admin
from django.utils.html import format_html
from django.urls import reverse

from .models import (
    ParentLedger,
    Ledger,
    TallyConfig,
    TallyVendorBill,
    TallyVendorAnalyzedBill,
    TallyVendorAnalyzedProduct,
    TallyExpenseBill,
)


class ParentLedgerAdmin(admin.ModelAdmin):
    list_display = ('parent', 'organization', 'created_at', 'updated_at')
    list_filter = ('organization', 'created_at')
    search_fields = ('parent', 'organization__name')
    readonly_fields = ('created_at', 'updated_at')


class LedgerAdmin(admin.ModelAdmin):
    list_display = ('name', 'parent', 'master_id', 'alter_id', 'organization', 'created_at')
    list_filter = ('parent', 'organization', 'created_at')
    search_fields = ('name', 'master_id', 'parent__parent', 'organization__name')
    readonly_fields = ('created_at', 'updated_at')
    autocomplete_fields = ('parent',)


class TallyConfigAdmin(admin.ModelAdmin):
    filter_horizontal = (
        'igst_parents',
        'cgst_parents',
        'sgst_parents',
        'vendor_parents',
        'chart_of_accounts_parents',
        'chart_of_accounts_expense_parents',
    )
    list_display = ('organization', 'display_mappings')
    search_fields = ('organization__name',)

    def display_mappings(self, obj):
        """Display a summary of the number of ledger mappings"""
        return format_html(
            "IGST: {}, CGST: {}, SGST: {}, Vendors: {}, COA: {}, Expense COA: {}",
            obj.igst_parents.count(),
            obj.cgst_parents.count(),
            obj.sgst_parents.count(),
            obj.vendor_parents.count(),
            obj.chart_of_accounts_parents.count(),
            obj.chart_of_accounts_expense_parents.count(),
        )
    display_mappings.short_description = "Mappings"


class TallyVendorAnalyzedProductInline(admin.TabularInline):
    model = TallyVendorAnalyzedProduct
    extra = 0
    fields = ('item_name', 'item_details', 'taxes', 'price', 'quantity', 'amount', 'product_gst')
    readonly_fields = ('created_at',)


class TallyVendorAnalyzedBillAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'vendor', 'bill_no', 'bill_date', 'total', 'gst_type', 'organization')
    list_filter = ('organization', 'gst_type', 'created_at')
    search_fields = ('bill_no', 'vendor__name', 'selected_bill__bill_munshi_name')
    readonly_fields = ('created_at',)
    inlines = [TallyVendorAnalyzedProductInline]
    fieldsets = (
        (None, {
            'fields': ('selected_bill', 'vendor', 'bill_no', 'bill_date', 'note')
        }),
        ('GST Details', {
            'fields': ('gst_type', 'total', 'igst', 'igst_taxes', 'cgst', 'cgst_taxes', 'sgst', 'sgst_taxes')
        }),
        ('Meta', {
            'fields': ('organization', 'created_at')
        }),
    )


class TallyVendorBillAdmin(admin.ModelAdmin):
    list_display = ('bill_munshi_name', 'status', 'file_type', 'organization', 'display_file', 'created_at')
    list_filter = ('status', 'file_type', 'organization', 'created_at')
    search_fields = ('bill_munshi_name', 'organization__name')
    readonly_fields = ('created_at', 'updated_at')

    def display_file(self, obj):
        if obj.file:
            return format_html('<a href="{}" target="_blank">View File</a>', obj.file.url)
        return "-"
    display_file.short_description = "File"


class TallyExpenseBillAdmin(admin.ModelAdmin):
    list_display = ('bill_munshi_name', 'status', 'file_type', 'organization', 'display_file', 'created_at')
    list_filter = ('status', 'file_type', 'organization', 'created_at')
    search_fields = ('bill_munshi_name', 'organization__name')
    readonly_fields = ('created_at', 'updated_at')

    def display_file(self, obj):
        if obj.file:
            return format_html('<a href="{}" target="_blank">View File</a>', obj.file.url)
        return "-"
    display_file.short_description = "File"


# Register models with the admin site
admin.site.register(ParentLedger, ParentLedgerAdmin)
admin.site.register(Ledger, LedgerAdmin)
admin.site.register(TallyConfig, TallyConfigAdmin)
admin.site.register(TallyVendorBill, TallyVendorBillAdmin)
admin.site.register(TallyVendorAnalyzedBill, TallyVendorAnalyzedBillAdmin)
admin.site.register(TallyExpenseBill, TallyExpenseBillAdmin)

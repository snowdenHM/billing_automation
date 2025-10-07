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
    TallyExpenseAnalyzedProduct, StockItem
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
    list_display = ('organization', 'display_mappings', 'display_parent_ledgers')
    list_filter = ('organization',)
    search_fields = ('organization__name',)
    ordering = ('organization__name',)

    def get_queryset(self, request):
        """Filter queryset based on user permissions and prefetch related data"""
        qs = super().get_queryset(request)
        return qs.select_related('organization').prefetch_related(
            'igst_parents',
            'cgst_parents',
            'sgst_parents',
            'vendor_parents',
            'chart_of_accounts_parents',
            'chart_of_accounts_expense_parents'
        )

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        """Filter ManyToMany fields by organization context"""
        if hasattr(request, '_obj_'):
            # If editing existing object, filter by its organization
            org = request._obj_.organization
        else:
            # For new objects, try to get organization from GET params or user
            org_id = request.GET.get('organization')
            if org_id:
                try:
                    from apps.organizations.models import Organization
                    org = Organization.objects.get(id=org_id)
                except (Organization.DoesNotExist, ValueError):
                    org = None
            else:
                org = None

        if org and db_field.name in ['igst_parents', 'cgst_parents', 'sgst_parents',
                                   'vendor_parents', 'chart_of_accounts_parents',
                                   'chart_of_accounts_expense_parents']:
            kwargs["queryset"] = ParentLedger.objects.filter(organization=org)

        return super().formfield_for_manytomany(db_field, request, **kwargs)

    def get_form(self, request, obj=None, **kwargs):
        """Store the object in request for use in formfield_for_manytomany"""
        request._obj_ = obj
        return super().get_form(request, obj, **kwargs)

    def display_mappings(self, obj):
        """Display a summary of the number of ledger mappings"""
        return format_html(
            "<strong>IGST:</strong> {} | <strong>CGST:</strong> {} | <strong>SGST:</strong> {} | <strong>Vendors:</strong> {} | <strong>COA:</strong> {} | <strong>Expense COA:</strong> {}",
            obj.igst_parents.count(),
            obj.cgst_parents.count(),
            obj.sgst_parents.count(),
            obj.vendor_parents.count(),
            obj.chart_of_accounts_parents.count(),
            obj.chart_of_accounts_expense_parents.count(),
        )
    display_mappings.short_description = "Configuration Summary"

    def display_parent_ledgers(self, obj):
        """Display detailed parent ledger information for organization context"""
        details = []

        # IGST Parents
        igst_names = [parent.parent for parent in obj.igst_parents.all()[:3]]
        if igst_names:
            igst_display = ", ".join(igst_names)
            if obj.igst_parents.count() > 3:
                igst_display += f" (+{obj.igst_parents.count() - 3} more)"
            details.append(f"<strong>IGST:</strong> {igst_display}")

        # CGST Parents
        cgst_names = [parent.parent for parent in obj.cgst_parents.all()[:3]]
        if cgst_names:
            cgst_display = ", ".join(cgst_names)
            if obj.cgst_parents.count() > 3:
                cgst_display += f" (+{obj.cgst_parents.count() - 3} more)"
            details.append(f"<strong>CGST:</strong> {cgst_display}")

        # SGST Parents
        sgst_names = [parent.parent for parent in obj.sgst_parents.all()[:3]]
        if sgst_names:
            sgst_display = ", ".join(sgst_names)
            if obj.sgst_parents.count() > 3:
                sgst_display += f" (+{obj.sgst_parents.count() - 3} more)"
            details.append(f"<strong>SGST:</strong> {sgst_display}")

        # Vendor Parents
        vendor_names = [parent.parent for parent in obj.vendor_parents.all()[:3]]
        if vendor_names:
            vendor_display = ", ".join(vendor_names)
            if obj.vendor_parents.count() > 3:
                vendor_display += f" (+{obj.vendor_parents.count() - 3} more)"
            details.append(f"<strong>Vendors:</strong> {vendor_display}")

        return format_html("<br>".join(details)) if details else "No mappings configured"

    display_parent_ledgers.short_description = "Parent Ledger Details"

    fieldsets = (
        ('Organization', {
            'fields': ('organization',),
            'description': 'Select the organization for this Tally configuration.'
        }),
        ('GST Parent Ledger Mappings', {
            'fields': ('igst_parents', 'cgst_parents', 'sgst_parents'),
            'description': 'Map parent ledgers for different GST types. These will be used for GST calculations and reporting.'
        }),
        ('Business Parent Ledger Mappings', {
            'fields': ('vendor_parents', 'chart_of_accounts_parents', 'chart_of_accounts_expense_parents'),
            'description': 'Map parent ledgers for vendors and chart of accounts categorization.'
        }),
    )


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
    list_display = ('bill_munshi_name', 'status', 'file_type', 'uploaded_by', 'organization', 'display_file', 'created_at')
    list_filter = ('status', 'file_type', 'uploaded_by', 'organization', 'created_at')
    search_fields = ('bill_munshi_name', 'uploaded_by__username', 'uploaded_by__first_name', 'uploaded_by__last_name', 'organization__name')
    readonly_fields = ('created_at', 'updated_at')
    fields = ('bill_munshi_name', 'file', 'file_type', 'status', 'process', 'uploaded_by', 'organization', 'analysed_data', 'created_at', 'updated_at')
    autocomplete_fields = ('uploaded_by', 'organization')

    def display_file(self, obj):
        if obj.file:
            return format_html('<a href="{}" target="_blank">View File</a>', obj.file.url)
        return "-"

    display_file.short_description = "File"


class TallyExpenseBillAdmin(admin.ModelAdmin):
    list_display = ('bill_munshi_name', 'status', 'file_type', 'uploaded_by', 'organization', 'display_file', 'created_at')
    list_filter = ('status', 'file_type', 'uploaded_by', 'organization', 'created_at')
    search_fields = ('bill_munshi_name', 'uploaded_by__username', 'uploaded_by__first_name', 'uploaded_by__last_name', 'organization__name')
    readonly_fields = ('created_at', 'updated_at')
    fields = ('bill_munshi_name', 'file', 'file_type', 'status', 'process', 'uploaded_by', 'organization', 'analysed_data', 'created_at', 'updated_at')
    autocomplete_fields = ('uploaded_by', 'organization')

    def display_file(self, obj):
        if obj.file:
            return format_html('<a href="{}" target="_blank">View File</a>', obj.file.url)
        return "-"

    display_file.short_description = "File"


class TallyExpenseAnalyzedProductInline(admin.TabularInline):
    model = TallyExpenseAnalyzedProduct
    extra = 0
    fields = ('item_details', 'chart_of_accounts', 'amount', 'debit_or_credit')
    readonly_fields = ('created_at',)


class TallyExpenseAnalyzedBillAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'vendor', 'bill_no', 'bill_date', 'total', 'organization')
    list_filter = ('organization', 'created_at')
    search_fields = ('bill_no', 'vendor__name', 'selected_bill__bill_munshi_name', 'voucher')
    readonly_fields = ('created_at',)
    inlines = [TallyExpenseAnalyzedProductInline]
    fieldsets = (
        (None, {
            'fields': ('selected_bill', 'vendor', 'voucher', 'bill_no', 'bill_date', 'note')
        }),
        ('GST Details', {
            'fields': ('total', 'igst', 'igst_taxes', 'cgst', 'cgst_taxes', 'sgst', 'sgst_taxes')
        }),
        ('Meta', {
            'fields': ('organization', 'created_at')
        }),
    )


# Register models with the admin site
admin.site.register(StockItem)
admin.site.register(ParentLedger, ParentLedgerAdmin)
admin.site.register(Ledger, LedgerAdmin)
admin.site.register(TallyConfig, TallyConfigAdmin)
admin.site.register(TallyVendorBill, TallyVendorBillAdmin)
admin.site.register(TallyVendorAnalyzedBill, TallyVendorAnalyzedBillAdmin)
admin.site.register(TallyExpenseBill, TallyExpenseBillAdmin)
admin.site.register(TallyExpenseAnalyzedBill, TallyExpenseAnalyzedBillAdmin)
admin.site.register(TallyExpenseAnalyzedProduct)

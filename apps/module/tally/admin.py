from django.contrib import admin
from django.utils.html import format_html
from django.http import JsonResponse
from django.urls import path, reverse
from django.utils.safestring import mark_safe

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
    autocomplete_fields = ('organization',)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "organization":
            # Only show organizations that the user has access to
            if not request.user.is_superuser:
                kwargs["queryset"] = request.user.organizations.all()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


class LedgerAdmin(admin.ModelAdmin):
    list_display = ('name', 'parent', 'master_id', 'alter_id', 'organization', 'created_at')
    list_filter = ('organization', 'parent', 'created_at')
    search_fields = ('name', 'master_id', 'parent__parent', 'organization__name')
    readonly_fields = ('created_at', 'updated_at')
    autocomplete_fields = ('organization',)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "parent":
            # Filter parent ledgers by organization if editing existing object
            obj_id = request.resolver_match.kwargs.get('object_id')
            if obj_id:
                try:
                    ledger = Ledger.objects.get(pk=obj_id)
                    kwargs["queryset"] = ParentLedger.objects.filter(organization=ledger.organization)
                except Ledger.DoesNotExist:
                    pass
            else:
                # For new objects, get organization from GET params
                org_id = request.GET.get('organization')
                if org_id:
                    kwargs["queryset"] = ParentLedger.objects.filter(organization_id=org_id)
                else:
                    kwargs["queryset"] = ParentLedger.objects.none()
        elif db_field.name == "organization":
            if not request.user.is_superuser:
                kwargs["queryset"] = request.user.organizations.all()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    class Media:
        js = ('admin/js/dependent_dropdown.js',)


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
    autocomplete_fields = ('organization',)

    def get_queryset(self, request):
        """Filter queryset based on user permissions and prefetch related data"""
        qs = super().get_queryset(request)
        qs = qs.select_related('organization').prefetch_related(
            'igst_parents',
            'cgst_parents',
            'sgst_parents',
            'vendor_parents',
            'chart_of_accounts_parents',
            'chart_of_accounts_expense_parents'
        )

        # Filter by user organization if not superuser
        if not request.user.is_superuser:
            qs = qs.filter(organization__in=request.user.organizations.all())

        return qs

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('get-parent-ledgers/', self.admin_site.admin_view(self.get_parent_ledgers),
                 name='tally_tallyconfig_get_parent_ledgers'),
        ]
        return custom_urls + urls

    def get_parent_ledgers(self, request):
        """AJAX endpoint to get parent ledgers for an organization"""
        org_id = request.GET.get('org_id')
        if org_id:
            parent_ledgers = ParentLedger.objects.filter(organization_id=org_id).values('id', 'parent')
            return JsonResponse({'parent_ledgers': list(parent_ledgers)})
        return JsonResponse({'parent_ledgers': []})

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "organization":
            if not request.user.is_superuser:
                kwargs["queryset"] = request.user.organizations.all()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        """Filter ManyToMany fields by organization context"""
        if hasattr(request, '_obj_') and request._obj_ is not None:
            # If editing existing object, filter by its organization
            org = request._obj_.organization
        else:
            # For new objects, try to get organization from GET params
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
        elif db_field.name in ['igst_parents', 'cgst_parents', 'sgst_parents',
                              'vendor_parents', 'chart_of_accounts_parents',
                              'chart_of_accounts_expense_parents']:
            # Show empty queryset if no organization is selected
            kwargs["queryset"] = ParentLedger.objects.none()

        return super().formfield_for_manytomany(db_field, request, **kwargs)

    def get_form(self, request, obj=None, **kwargs):
        """Store the object in request for use in formfield_for_manytomany"""
        request._obj_ = obj
        form = super().get_form(request, obj, **kwargs)

        # Ensure the form has Media class and add our JavaScript
        if not hasattr(form, 'Media'):
            class Media:
                js = ('admin/js/tally_dependent_dropdown.js',)
            form.Media = Media
        else:
            # If Media exists, extend the js tuple
            existing_js = getattr(form.Media, 'js', ())
            if isinstance(existing_js, (list, tuple)):
                new_js = list(existing_js) + ['admin/js/tally_dependent_dropdown.js']
            else:
                new_js = ['admin/js/tally_dependent_dropdown.js']
            form.Media.js = tuple(new_js)

        return form

    def render_change_form(self, request, context, *args, **kwargs):
        """Add organization change URL to context for JavaScript"""
        context['get_parent_ledgers_url'] = reverse('admin:tally_tallyconfig_get_parent_ledgers')
        return super().render_change_form(request, context, *args, **kwargs)

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

    class Media:
        js = ('admin/js/tally_dependent_dropdown.js',)


# Enhanced admin classes for other models with organization filtering
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
    autocomplete_fields = ('organization',)

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

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if not request.user.is_superuser:
            qs = qs.filter(organization__in=request.user.organizations.all())
        return qs


class TallyVendorBillAdmin(admin.ModelAdmin):
    list_display = ('bill_munshi_name', 'status', 'file_type', 'uploaded_by', 'organization', 'display_file', 'created_at')
    list_filter = ('status', 'file_type', 'uploaded_by', 'organization', 'created_at')
    search_fields = ('bill_munshi_name', 'uploaded_by__username', 'uploaded_by__first_name', 'uploaded_by__last_name', 'organization__name')
    readonly_fields = ('created_at', 'updated_at')
    fields = ('bill_munshi_name', 'file', 'file_type', 'status', 'process', 'uploaded_by', 'organization', 'analysed_data', 'created_at', 'updated_at')
    autocomplete_fields = ('uploaded_by', 'organization')

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if not request.user.is_superuser:
            qs = qs.filter(organization__in=request.user.organizations.all())
        return qs

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

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if not request.user.is_superuser:
            qs = qs.filter(organization__in=request.user.organizations.all())
        return qs

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
    autocomplete_fields = ('organization',)

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

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if not request.user.is_superuser:
            qs = qs.filter(organization__in=request.user.organizations.all())
        return qs


class StockItemAdmin(admin.ModelAdmin):
    list_display = ('name', 'parent', 'unit', 'category', 'organization', 'created_at')
    list_filter = ('organization', 'category', 'gst_applicable', 'created_at')
    search_fields = ('name', 'item_code', 'alias', 'organization__name')
    readonly_fields = ('created_at', 'updated_at')
    autocomplete_fields = ('organization',)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if not request.user.is_superuser:
            qs = qs.filter(organization__in=request.user.organizations.all())
        return qs


# Register models with the admin site
admin.site.register(StockItem, StockItemAdmin)
admin.site.register(ParentLedger, ParentLedgerAdmin)
admin.site.register(Ledger, LedgerAdmin)
admin.site.register(TallyConfig, TallyConfigAdmin)
admin.site.register(TallyVendorBill, TallyVendorBillAdmin)
admin.site.register(TallyVendorAnalyzedBill, TallyVendorAnalyzedBillAdmin)
admin.site.register(TallyExpenseBill, TallyExpenseBillAdmin)
admin.site.register(TallyExpenseAnalyzedBill, TallyExpenseAnalyzedBillAdmin)

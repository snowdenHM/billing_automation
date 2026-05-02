from django.contrib import admin
from django.utils.html import format_html
from django.http import JsonResponse
from django.urls import path, reverse
from django.utils.safestring import mark_safe

from apps.common.admin import BaseOrgAdmin, FileDisplayMixin, OwnershipDisplayMixin

from .models import (
    ParentLedger,
    Ledger,
    TallyConfig,
    TallyVendorBill,
    TallyVendorAnalyzedBill,
    TallyVendorAnalyzedProduct,
    TallyVendorConsolidatedProduct,
    TallyExpenseBill,
    TallyExpenseAnalyzedBill,
    TallyExpenseAnalyzedProduct,
    TallyExpenseConsolidatedProduct,
    StockItem
)
from .forms import TallyConfigForm


# ============================================================================
# Parent Ledger & Ledger Admin
# ============================================================================

@admin.register(ParentLedger)
class ParentLedgerAdmin(BaseOrgAdmin):
    """Admin interface for Tally Parent Ledgers."""
    
    list_display = ('parent', 'organization', 'created_at', 'updated_at')
    list_filter = ('organization', 'created_at')
    search_fields = ('parent', 'organization__name')
    autocomplete_fields = ('organization',)
    readonly_fields = ('created_at', 'updated_at')
    date_hierarchy = 'created_at'
    list_per_page = 50


@admin.register(Ledger)
class LedgerAdmin(BaseOrgAdmin):
    """Admin interface for Tally Ledgers."""
    
    list_display = ('name', 'parent', 'master_id', 'alter_id', 'organization', 'created_at')
    list_filter = ('organization', 'parent', 'created_at')
    search_fields = ('name', 'master_id', 'parent__parent', 'organization__name')
    autocomplete_fields = ('organization',)
    readonly_fields = ('created_at', 'updated_at')
    date_hierarchy = 'created_at'
    list_per_page = 50

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "parent":
            obj_id = request.resolver_match.kwargs.get('object_id')
            if obj_id:
                try:
                    ledger = Ledger.objects.get(pk=obj_id)
                    kwargs["queryset"] = ParentLedger.objects.filter(organization=ledger.organization)
                except Ledger.DoesNotExist:
                    pass
            else:
                org_id = request.GET.get('organization')
                if org_id:
                    kwargs["queryset"] = ParentLedger.objects.filter(organization_id=org_id)
                else:
                    kwargs["queryset"] = ParentLedger.objects.none()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    class Media:
        js = ('admin/js/dependent_dropdown.js',)


# ============================================================================
# Tally Configuration Admin
# ============================================================================

@admin.register(TallyConfig)
class TallyConfigAdmin(BaseOrgAdmin):
    """Admin interface for Tally Configuration."""
    
    form = TallyConfigForm
    list_display = ('organization', 'display_mappings', 'display_parent_ledgers')
    list_filter = ('organization',)
    search_fields = ('organization__name',)
    ordering = ('organization__name',)
    autocomplete_fields = ('organization',)
    list_per_page = 50

    fieldsets = (
        ('Organization', {
            'fields': ('organization',)
        }),
        ('Product Sync Settings', {
            'fields': ('tally_product_allow_sync',),
            'description': 'Configure whether product synchronization is allowed for this organization.'
        }),
        ('GST Parent Ledger Mappings', {
            'fields': ('igst_parents', 'cgst_parents', 'sgst_parents'),
            'description': 'Map parent ledgers for different GST types. These will be used for GST calculations and reporting.'
        }),
        ('Vendor & Chart of Accounts Mappings', {
            'fields': ('vendor_parents', 'chart_of_accounts_parents', 'chart_of_accounts_expense_parents'),
            'description': 'Map parent ledgers for vendor bills and chart of accounts.'
        }),
        ('TDS & Payment Mappings', {
            'fields': ('tds_parents', 'payment_parents'),
            'description': 'Map parent ledgers for TDS and payment transactions.'
        }),
        ('Round Off Mappings', {
            'fields': ('round_off_parents',),
            'description': 'Map parent ledgers (e.g. Indirect Expenses) used to source the Round Off ledger applied during Tally sync.'
        }),
    )

    def get_queryset(self, request):
        """Filter queryset based on user permissions and prefetch related data"""
        qs = super().get_queryset(request)
        return qs.prefetch_related(
            'igst_parents',
            'cgst_parents',
            'sgst_parents',
            'vendor_parents',
            'chart_of_accounts_parents',
            'chart_of_accounts_expense_parents',
            'tds_parents',
            'payment_parents',
            'round_off_parents',
        )

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

    def get_form(self, request, obj=None, **kwargs):
        """Use custom form and add JavaScript for dynamic updates"""
        form = super().get_form(request, obj, **kwargs)

        # Add organization to form initial data if creating new object
        if not obj and 'organization' in request.GET:
            form.base_fields['organization'].initial = request.GET['organization']

        # Add Media class for JavaScript
        class Media:
            js = ('admin/js/tally_simple_dropdown.js',)
            css = {
                'all': ('admin/css/tally_config.css',)
            }
        form.Media = Media

        return form

    def render_change_form(self, request, context, *args, **kwargs):
        """Add organization change URL to context for JavaScript"""
        context['get_parent_ledgers_url'] = reverse('admin:tally_tallyconfig_get_parent_ledgers')
        return super().render_change_form(request, context, *args, **kwargs)

    @admin.display(description="Configuration Summary")
    def display_mappings(self, obj):
        """Display a summary of the number of ledger mappings"""
        return format_html(
            "<strong>IGST:</strong> {} | <strong>CGST:</strong> {} | <strong>SGST:</strong> {} | <strong>Vendors:</strong> {} | <strong>COA:</strong> {} | <strong>Expense COA:</strong> {} | <strong>TDS:</strong> {} | <strong>Payment:</strong> {}",
            obj.igst_parents.count(),
            obj.cgst_parents.count(),
            obj.sgst_parents.count(),
            obj.vendor_parents.count(),
            obj.chart_of_accounts_parents.count(),
            obj.chart_of_accounts_expense_parents.count(),
            obj.tds_parents.count(),
            obj.payment_parents.count(),
        )

    @admin.display(description="Parent Ledger Details")
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

        # TDS Parents
        tds_names = [parent.parent for parent in obj.tds_parents.all()[:3]]
        if tds_names:
            tds_display = ", ".join(tds_names)
            if obj.tds_parents.count() > 3:
                tds_display += f" (+{obj.tds_parents.count() - 3} more)"
            details.append(f"<strong>TDS:</strong> {tds_display}")

        # Payment Parents
        payment_names = [parent.parent for parent in obj.payment_parents.all()[:3]]
        if payment_names:
            payment_display = ", ".join(payment_names)
            if obj.payment_parents.count() > 3:
                payment_display += f" (+{obj.payment_parents.count() - 3} more)"
            details.append(f"<strong>Payment:</strong> {payment_display}")

        return format_html("<br>".join(details)) if details else "No mappings configured"

    class Media:
        js = ('admin/js/tally_dependent_dropdown.js',)


# ============================================================================
# Vendor Bill Related Admin
# ============================================================================
class TallyVendorAnalyzedProductInline(admin.TabularInline):
    """Inline admin for Vendor Analyzed Products."""
    
    model = TallyVendorAnalyzedProduct
    extra = 0
    fields = (
        'item_name', 'item_details', 'taxes', 'price', 'quantity', 'amount',
        'product_gst',
        'cgst', 'cgst_ledger',
        'sgst', 'sgst_ledger',
        'igst', 'igst_ledger',
    )
    readonly_fields = ('created_at', 'igst', 'cgst', 'sgst')
    autocomplete_fields = ('taxes', 'cgst_ledger', 'sgst_ledger', 'igst_ledger')


@admin.register(TallyVendorAnalyzedBill)
class TallyVendorAnalyzedBillAdmin(BaseOrgAdmin):
    """Admin interface for Vendor Analyzed Bills."""
    
    list_display = ('__str__', 'vendor', 'bill_no', 'bill_date', 'due_date', 'total', 'discount', 'gst_type', 'organization')
    list_filter = ('organization', 'gst_type', 'created_at', 'bill_date')
    search_fields = ('bill_no', 'vendor__name', 'selected_bill__bill_munshi_name')
    inlines = [TallyVendorAnalyzedProductInline]
    autocomplete_fields = ('organization', 'vendor', 'selected_bill')
    readonly_fields = ('created_at',)
    date_hierarchy = 'created_at'
    list_per_page = 50

    fieldsets = (
        ('Bill Information', {
            'fields': ('selected_bill', 'vendor', 'bill_no', 'bill_date', 'due_date', 'note')
        }),
        ('GST Details', {
            'fields': ('gst_type', 'total', 'igst', 'igst_taxes', 'cgst', 'cgst_taxes', 'sgst', 'sgst_taxes')
        }),
        ('Discount Details', {
            'fields': ('discount', 'discount_taxes'),
            'description': 'Discount amount and associated ledger mapping.'
        }),
        ('Round Off', {
            'fields': ('round_off', 'round_off_taxes'),
            'description': 'Auto-computed during verify when |total − (subtotal + GST + cess + freight − discount)| < ₹1.'
        }),
        ('Metadata', {
            'fields': ('organization', 'created_at'),
            'classes': ('collapse',)
        }),
    )

    def get_queryset(self, request):
        """Optimize queryset with select_related."""
        qs = super().get_queryset(request)
        return qs.select_related('organization', 'vendor', 'selected_bill')


class _TallyBillAdminBase(FileDisplayMixin, OwnershipDisplayMixin, BaseOrgAdmin):
    """Shared base for TallyVendorBillAdmin and TallyExpenseBillAdmin."""
    
    list_display = (
        'bill_munshi_name', 'status', 'display_ownership', 'tally_synced', 
        'file_type', 'is_duplicate', 'is_processing', 'uploaded_by', 
        'organization', 'display_file', 'created_at'
    )
    list_filter = (
        'status', 'bill_belong_your_org', 'tally_synced', 'file_type', 
        'is_duplicate', 'is_processing', 'uploaded_by', 'organization', 'created_at'
    )
    search_fields = (
        'bill_munshi_name', 'description', 'uploaded_by__username', 
        'uploaded_by__first_name', 'uploaded_by__last_name', 'organization__name'
    )
    readonly_fields = (
        'analysed_data', 'created_at', 'updated_at', 'duplicate_matched_bills', 'processing_error'
    )
    date_hierarchy = 'created_at'
    list_per_page = 50
    autocomplete_fields = ('uploaded_by', 'organization')
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('bill_munshi_name', 'file', 'file_type', 'status', 'description')
        }),
        ('Ownership & Sync', {
            'fields': ('bill_belong_your_org', 'tally_synced', 'process')
        }),
        ('Processing Status', {
            'fields': ('is_processing', 'processing_error', 'analysed_data')
        }),
        ('Duplicate Detection', {
            'fields': ('is_duplicate', 'duplicate_description', 'duplicate_score', 'duplicate_matched_bills'),
            'classes': ('collapse',)
        }),
        ('Metadata', {
            'fields': ('uploaded_by', 'organization', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def get_queryset(self, request):
        """Optimize queryset with select_related."""
        qs = super().get_queryset(request)
        return qs.select_related('uploaded_by', 'organization')

    class Meta:
        abstract = True


@admin.register(TallyVendorBill)
class TallyVendorBillAdmin(_TallyBillAdminBase):
    """Admin interface for Tally Vendor Bills."""
    pass


@admin.register(TallyExpenseBill)
class TallyExpenseBillAdmin(_TallyBillAdminBase):
    """Admin interface for Tally Expense Bills."""
    pass


# ============================================================================
# Expense Bill Related Admin
# ============================================================================

class TallyExpenseAnalyzedProductInline(admin.TabularInline):
    """Inline admin for Expense Analyzed Products."""
    
    model = TallyExpenseAnalyzedProduct
    extra = 0
    fields = ('item_details', 'chart_of_accounts', 'amount', 'debit_or_credit')
    readonly_fields = ('created_at',)


@admin.register(TallyExpenseAnalyzedBill)
class TallyExpenseAnalyzedBillAdmin(BaseOrgAdmin):
    """Admin interface for Expense Analyzed Bills."""
    
    list_display = ('__str__', 'vendor', 'bill_no', 'bill_date', 'due_date', 'total', 'organization')
    list_filter = ('organization', 'created_at', 'bill_date')
    search_fields = ('bill_no', 'vendor__name', 'selected_bill__bill_munshi_name', 'voucher')
    inlines = [TallyExpenseAnalyzedProductInline]
    autocomplete_fields = ('organization', 'vendor', 'selected_bill')
    readonly_fields = ('created_at',)
    date_hierarchy = 'created_at'
    list_per_page = 50

    fieldsets = (
        ('Bill Information', {
            'fields': ('selected_bill', 'vendor', 'voucher', 'bill_no', 'bill_date', 'due_date', 'note')
        }),
        ('GST Details', {
            'fields': ('total', 'igst', 'igst_taxes', 'cgst', 'cgst_taxes', 'sgst', 'sgst_taxes')
        }),
        ('TDS & Other Adjustments', {
            'fields': ('tds', 'tds_taxes', 'other_adjustment', 'other_adjustment_taxes')
        }),
        ('Round Off', {
            'fields': ('round_off', 'round_off_taxes', 'round_off_debit_or_credit'),
            'description': 'Auto-computed during verify when |DR − CR| < ₹1. Side is set automatically to balance the journal entry.'
        }),
        ('Metadata', {
            'fields': ('organization', 'created_at'),
            'classes': ('collapse',)
        }),
    )

    def get_queryset(self, request):
        """Optimize queryset with select_related."""
        qs = super().get_queryset(request)
        return qs.select_related('organization', 'vendor', 'selected_bill')


# ============================================================================
# Consolidated Products Admin
# ============================================================================

@admin.register(TallyVendorConsolidatedProduct)
class TallyVendorConsolidatedProductAdmin(BaseOrgAdmin):
    """Admin interface for Vendor Consolidated Products."""
    
    list_display = ('vendor_bill_name', 'item_name_short', 'amount', 'product_gst', 'original_items_count', 'organization', 'created_at')
    list_filter = ('organization', 'product_gst', 'created_at')
    search_fields = ('item_name', 'item_details', 'vendor_bill_analyzed__selected_bill__bill_munshi_name', 'organization__name')
    readonly_fields = ('igst', 'cgst', 'sgst', 'created_at', 'updated_at')
    date_hierarchy = 'created_at'
    list_per_page = 50
    autocomplete_fields = ('organization', 'vendor_bill_analyzed', 'taxes')
    
    fieldsets = (
        ('Bill Information', {
            'fields': ('vendor_bill_analyzed', 'organization')
        }),
        ('Item Details', {
            'fields': ('item_name', 'item_details', 'taxes')
        }),
        ('Financial Details', {
            'fields': ('price', 'quantity', 'amount', 'product_gst')
        }),
        ('GST Breakdown', {
            'fields': ('igst', 'cgst', 'sgst'),
            'classes': ('collapse',)
        }),
        ('Consolidation Metadata', {
            'fields': ('original_items_count', 'consolidation_notes')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    @admin.display(description='Vendor Bill', ordering='vendor_bill_analyzed__selected_bill__bill_munshi_name')
    def vendor_bill_name(self, obj):
        """Display vendor bill name."""
        if obj.vendor_bill_analyzed and obj.vendor_bill_analyzed.selected_bill:
            return obj.vendor_bill_analyzed.selected_bill.bill_munshi_name
        return 'N/A'
    
    @admin.display(description='Item Name')
    def item_name_short(self, obj):
        """Display shortened item name."""
        if obj.item_name:
            return obj.item_name[:50] + '...' if len(obj.item_name) > 50 else obj.item_name
        return 'N/A'
    
    def get_queryset(self, request):
        """Optimize queryset with select_related."""
        qs = super().get_queryset(request)
        return qs.select_related('organization', 'vendor_bill_analyzed', 'vendor_bill_analyzed__selected_bill')


@admin.register(TallyExpenseConsolidatedProduct)
class TallyExpenseConsolidatedProductAdmin(BaseOrgAdmin):
    """Admin interface for Expense Consolidated Products."""
    
    list_display = (
        'expense_bill_name', 'item_details_short', 'amount', 'debit_or_credit', 
        'original_entries_count', 'organization', 'created_at'
    )
    list_filter = ('organization', 'debit_or_credit', 'created_at')
    search_fields = ('item_details', 'expense_bill__selected_bill__bill_munshi_name', 'organization__name')
    readonly_fields = ('created_at', 'updated_at')
    date_hierarchy = 'created_at'
    list_per_page = 50
    autocomplete_fields = ('organization', 'expense_bill', 'chart_of_accounts')
    
    fieldsets = (
        ('Bill Information', {
            'fields': ('expense_bill', 'organization')
        }),
        ('Item Details', {
            'fields': ('item_details', 'chart_of_accounts', 'debit_or_credit')
        }),
        ('Financial Details', {
            'fields': ('amount',)
        }),
        ('Consolidation Metadata', {
            'fields': ('original_entries_count', 'consolidation_notes')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    @admin.display(description='Expense Bill', ordering='expense_bill__selected_bill__bill_munshi_name')
    def expense_bill_name(self, obj):
        """Display expense bill name."""
        if obj.expense_bill and obj.expense_bill.selected_bill:
            return obj.expense_bill.selected_bill.bill_munshi_name
        return 'N/A'
    
    @admin.display(description='Item Details')
    def item_details_short(self, obj):
        """Display shortened item details."""
        if obj.item_details:
            return obj.item_details[:50] + '...' if len(obj.item_details) > 50 else obj.item_details
        return 'N/A'
    
    def get_queryset(self, request):
        """Optimize queryset with select_related."""
        qs = super().get_queryset(request)
        return qs.select_related('organization', 'expense_bill', 'expense_bill__selected_bill')


# ============================================================================
# Stock Item Admin
# ============================================================================

@admin.register(StockItem)
class StockItemAdmin(BaseOrgAdmin):
    """Admin interface for Tally Stock Items."""
    
    list_display = ('name', 'parent', 'unit', 'category', 'organization', 'created_at')
    list_filter = ('organization', 'category', 'gst_applicable', 'created_at')
    search_fields = ('name', 'item_code', 'alias', 'organization__name')
    readonly_fields = ('created_at', 'updated_at')
    date_hierarchy = 'created_at'
    list_per_page = 50
    autocomplete_fields = ('organization',)
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('name', 'item_code', 'alias', 'parent', 'category')
        }),
        ('Unit & GST', {
            'fields': ('unit', 'gst_applicable')
        }),
        ('Metadata', {
            'fields': ('organization', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


# ============================================================================
# Tally Setup Step Admin (admin-managed setup guide shown on /tally/account-info)
# ============================================================================

from .models import TallySetupStep


@admin.register(TallySetupStep)
class TallySetupStepAdmin(admin.ModelAdmin):
    """
    Manage the Tally setup walkthrough that appears on the Account Info page.
    """

    list_display = (
        "step_number",
        "title",
        "image_thumb",
        "is_active",
        "order",
        "updated_at",
    )
    list_display_links = ("step_number", "title")
    list_filter = ("is_active",)
    search_fields = ("title", "description", "image_alt")
    list_editable = ("is_active", "order")
    ordering = ("step_number", "order")
    list_per_page = 50
    readonly_fields = ("id", "created_at", "updated_at", "image_thumb_large")

    fieldsets = (
        ("Step content", {
            "fields": ("step_number", "title", "description"),
        }),
        ("Image", {
            "fields": ("image", "image_alt", "image_thumb_large"),
        }),
        ("Display options", {
            "fields": ("order", "is_active"),
        }),
        ("Metadata", {
            "fields": ("id", "created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )

    def image_thumb(self, obj):
        if obj.image:
            return format_html(
                '<img src="{}" style="height:36px;width:auto;border-radius:4px;'
                'box-shadow:0 0 0 1px rgba(15,23,42,.08);" alt="" />',
                obj.image.url,
            )
        return "—"
    image_thumb.short_description = "Preview"

    def image_thumb_large(self, obj):
        if obj.image:
            return format_html(
                '<img src="{}" style="max-height:240px;max-width:100%;border-radius:8px;'
                'box-shadow:0 1px 4px rgba(15,23,42,.12);" alt="" />',
                obj.image.url,
            )
        return mark_safe("<em>No image uploaded.</em>")
    image_thumb_large.short_description = "Image preview"


# ============================================================================
# Tally TCP Release Admin (admin-managed TCP file served to users)
# ============================================================================

from .models import TallyTcpRelease


@admin.register(TallyTcpRelease)
class TallyTcpReleaseAdmin(admin.ModelAdmin):
    """
    Manage Tally TCP releases. Whichever release is marked 'is_active'
    is served to users on the Tally Account Info page.
    """

    list_display = (
        "version",
        "is_active",
        "file_link",
        "uploaded_by",
        "created_at",
        "updated_at",
    )
    list_display_links = ("version",)
    list_filter = ("is_active",)
    search_fields = ("version", "notes", "uploaded_by__email", "uploaded_by__full_name")
    list_editable = ("is_active",)
    ordering = ("-created_at",)
    readonly_fields = ("id", "uploaded_by", "created_at", "updated_at", "file_link")
    list_per_page = 50

    fieldsets = (
        ("Release", {
            "fields": ("version", "file", "file_link", "notes"),
        }),
        ("Status", {
            "fields": ("is_active",),
            "description": (
                "Only one TCP release should be active at any time. "
                "Saving a release with this box checked will automatically deactivate the others."
            ),
        }),
        ("Metadata", {
            "fields": ("id", "uploaded_by", "created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )

    def save_model(self, request, obj, form, change):
        # Track who uploaded a release (only on first save)
        if not change and not obj.uploaded_by_id:
            obj.uploaded_by = request.user
        super().save_model(request, obj, form, change)

    def file_link(self, obj):
        if obj.file:
            return format_html(
                '<a href="{}" target="_blank" rel="noopener" '
                'style="display:inline-flex;align-items:center;gap:.4rem;'
                'padding:.25rem .6rem;border-radius:6px;'
                'background:#eff6ff;color:#1d4ed8;'
                'font-weight:600;font-size:.8rem;text-decoration:none;'
                'border:1px solid #dbeafe;">'
                '↓ Download {}</a>',
                obj.file.url,
                obj.version,
            )
        return "—"
    file_link.short_description = "TCP file"


# ============================================================================
# GST Rate → Ledger Mapping Admin
# ============================================================================

from .models import GstRateLedgerMapping


@admin.register(GstRateLedgerMapping)
class GstRateLedgerMappingAdmin(BaseOrgAdmin):
    """Per-organization GST rate → CGST/SGST/IGST ledger mapping."""

    list_display = ("organization", "rate", "cgst_ledger", "sgst_ledger", "igst_ledger", "updated_at")
    list_filter = ("organization", "rate")
    search_fields = (
        "organization__name",
        "cgst_ledger__name",
        "sgst_ledger__name",
        "igst_ledger__name",
    )
    autocomplete_fields = ("cgst_ledger", "sgst_ledger", "igst_ledger")
    ordering = ("organization", "rate")

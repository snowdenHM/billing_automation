from django.contrib import admin
from django.utils.html import format_html
from unfold.admin import ModelAdmin as UnfoldModelAdmin
from .models import Organization, OrgMembership, OrganizationAPIKey, Module, OrganizationModule


# Django links only the FIRST column of list_display to the change page.
# Every admin here led with "id", so the only way into a record was a
# two-character number — operators clicked the name or email, nothing
# happened, and the detail page looked missing. Each admin below now names
# the column a human would actually click.
#
# list_select_related is set wherever list_display shows a FK: without it
# Django issues one extra query per row per FK, so a 50-row page was
# firing ~100 queries to render names it could have joined in one.


@admin.register(Organization)
class OrganizationAdmin(UnfoldModelAdmin):
    """Admin interface for Organization model."""
    
    list_display = ("name", "slug", "gst_number", "status", "owner", "created_by", "created_at")
    list_display_links = ("name",)
    list_select_related = ("owner", "created_by")
    search_fields = ("name", "slug", "gst_number", "owner__email", "created_by__email")
    list_filter = ("status", "created_at", "updated_at")
    readonly_fields = ("created_at", "updated_at")
    date_hierarchy = "created_at"
    list_per_page = 50
    autocomplete_fields = ("owner", "created_by")
    
    fieldsets = (
        ("Basic Information", {
            "fields": ("name", "slug", "gst_number")
        }),
        ("Status & Ownership", {
            "fields": ("status", "owner", "created_by")
        }),
        ("Timestamps", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",)
        }),
    )


@admin.register(OrgMembership)
class OrgMembershipAdmin(UnfoldModelAdmin):
    """Admin interface for Organization Membership."""
    
    list_display = ("organization", "user", "role", "is_active", "created_at")
    list_display_links = ("organization", "user")
    list_select_related = ("organization", "user")
    list_filter = ("role", "is_active", "created_at")
    search_fields = ("organization__name", "user__email", "user__first_name", "user__last_name")
    readonly_fields = ("created_at", "updated_at")
    date_hierarchy = "created_at"
    list_per_page = 50
    autocomplete_fields = ("organization", "user")
    
    fieldsets = (
        ("Membership Details", {
            "fields": ("organization", "user", "role", "is_active")
        }),
        ("Timestamps", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",)
        }),
    )


@admin.register(OrganizationAPIKey)
class OrganizationAPIKeyAdmin(UnfoldModelAdmin):
    """Admin interface for Organization API Keys."""
    
    list_display = ("name", "organization", "api_key_prefix", "created_by", "created_at")
    list_display_links = ("name",)
    list_select_related = ("organization", "created_by")
    search_fields = ("organization__name", "name", "created_by__email")
    list_filter = ("created_at",)
    readonly_fields = ("api_key", "created_at", "updated_at")
    date_hierarchy = "created_at"
    list_per_page = 50
    autocomplete_fields = ("organization", "created_by")
    
    fieldsets = (
        ("API Key Information", {
            "fields": ("organization", "name", "api_key")
        }),
        ("Metadata", {
            "fields": ("created_by", "created_at", "updated_at"),
            "classes": ("collapse",)
        }),
    )
    
    @admin.display(description="API Key Prefix", ordering="api_key")
    def api_key_prefix(self, obj):
        """Display API key prefix for identification."""
        if obj.api_key:
            return format_html(
                '<code>{}</code>',
                obj.api_key.prefix if hasattr(obj.api_key, 'prefix') else str(obj.api_key)[:8] + '...'
            )
        return "N/A"


@admin.register(Module)
class ModuleAdmin(UnfoldModelAdmin):
    """Admin interface for Module model."""
    
    list_display = ("code", "name", "created_at")
    list_display_links = ("code", "name")
    search_fields = ("code", "name")
    list_filter = ("created_at",)
    readonly_fields = ("created_at", "updated_at")
    date_hierarchy = "created_at"
    list_per_page = 50
    ordering = ("code",)
    
    fieldsets = (
        ("Module Information", {
            "fields": ("code", "name")
        }),
        ("Timestamps", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",)
        }),
    )
    

@admin.register(OrganizationModule)
class OrganizationModuleAdmin(UnfoldModelAdmin):
    """Admin interface for Organization Module assignments."""
    
    list_display = ("organization", "module", "is_active", "created_at")
    list_display_links = ("organization", "module")
    list_select_related = ("organization", "module")
    # ``module`` as a plain FK filter renders one <option> per row in the
    # table. RelatedOnlyFieldListFilter limits it to values actually in
    # use, which is both shorter and what the operator expects.
    list_filter = ("is_active", ("module", admin.RelatedOnlyFieldListFilter), "created_at")
    search_fields = ("organization__name", "module__code", "module__name")
    readonly_fields = ("created_at", "updated_at")
    date_hierarchy = "created_at"
    list_per_page = 50
    autocomplete_fields = ("organization", "module")
    
    fieldsets = (
        ("Module Assignment", {
            "fields": ("organization", "module", "is_active")
        }),
        ("Timestamps", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",)
        }),
    )

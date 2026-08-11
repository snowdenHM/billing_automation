from django.contrib import admin
from django.utils.html import format_html
from unfold.admin import ModelAdmin as UnfoldModelAdmin
from .models import Organization, OrgMembership, OrganizationAPIKey, Module, OrganizationModule


@admin.register(Organization)
class OrganizationAdmin(UnfoldModelAdmin):
    """Admin interface for Organization model."""
    
    list_display = ("id", "name", "slug", "gst_number", "status", "owner", "created_by", "created_at")
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
    
    list_display = ("id", "organization", "user", "role", "is_active", "created_at")
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
    
    list_display = ("id", "organization", "name", "api_key_prefix", "created_by", "created_at")
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
    
    list_display = ("id", "code", "name", "created_at")
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
    
    list_display = ("id", "organization", "module", "is_active", "created_at")
    list_filter = ("is_active", "module", "created_at")
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

from django.contrib import admin
from django.utils.html import format_html
from unfold.admin import ModelAdmin as UnfoldModelAdmin

from apps.common.admin import BaseOrgAdmin
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
#
# Every model in this app belongs to an organisation, so each admin extends
# BaseOrgAdmin rather than ModelAdmin: on a plain ModelAdmin a staff user who
# is not a superuser sees — and can edit — every tenant's memberships, API
# keys and module assignments. Today only superusers are staff, so nothing
# leaks right now; the point is that granting staff to one client's admin
# stops being an incident. ``Module`` is the exception: it is the
# deployment's catalogue of products, not a tenant's data.


@admin.register(Organization)
class OrganizationAdmin(BaseOrgAdmin):
    """Admin interface for Organization model."""
    
    # The tenant *is* the row here, so scope by primary key.
    org_lookup = "id"
    list_display = ("name", "slug", "gst_number", "status", "require_bill_approval",
                    "owner", "created_by", "created_at")
    list_display_links = ("name",)
    list_select_related = ("owner", "created_by")
    search_fields = ("name", "slug", "gst_number", "owner__email", "created_by__email")
    list_filter = ("status", "require_bill_approval", "created_at", "updated_at")
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
        ("Paid features", {
            "fields": ("require_bill_approval",),
            "description": (
                "Switches a client's workspace onto a feature they have paid "
                "for. Each one is off until it is turned on here, so a "
                "workspace never gains behaviour nobody asked for.<br><br>"
                "<b>Bill approval</b> makes a verified bill wait for a "
                "Manager or Admin before it can be posted to Tally or Zoho. "
                "With it off, whoever may post simply posts, which is how "
                "every workspace behaves today. Turning it on takes effect "
                "on the next bill \u2014 no restart, nothing to deploy \u2014 and "
                "turning it back off releases anything still waiting."
            ),
        }),
        ("Timestamps", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",)
        }),
    )


    actions = ("enable_bill_approval", "disable_bill_approval")

    @admin.action(description="Enable bill approval (paid feature)")
    def enable_bill_approval(self, request, queryset):
        n = queryset.update(require_bill_approval=True)
        self.message_user(
            request,
            f"Bill approval is on for {n} workspace(s). It applies to the "
            f"next bill; nothing already posted is affected.",
        )

    @admin.action(description="Disable bill approval")
    def disable_bill_approval(self, request, queryset):
        n = queryset.update(require_bill_approval=False)
        self.message_user(
            request,
            f"Bill approval is off for {n} workspace(s). Any bill still "
            f"waiting for a decision can now be posted without one.",
        )

@admin.register(OrgMembership)
class OrgMembershipAdmin(BaseOrgAdmin):
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
class OrganizationAPIKeyAdmin(BaseOrgAdmin):
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
class OrganizationModuleAdmin(BaseOrgAdmin):
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

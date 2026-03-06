from django.contrib import admin
from django.utils.html import format_html
from .models import Plan, Subscription


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    """Admin interface for Subscription Plans."""
    
    list_display = ("id", "code", "name", "max_users", "billing_cycle", "price_display", "is_active", "created_at")
    search_fields = ("code", "name", "description")
    list_filter = ("billing_cycle", "created_at")
    readonly_fields = ("created_at", "updated_at")
    date_hierarchy = "created_at"
    list_per_page = 50
    
    fieldsets = (
        ("Plan Details", {
            "fields": ("code", "name", "description", "max_users")
        }),
        ("Pricing", {
            "fields": ("billing_cycle", "price")
        }),
        ("Status", {
            "fields": ("is_active",)
        }),
        ("Timestamps", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",)
        }),
    )
    
    @admin.display(description="Price", ordering="price")
    def price_display(self, obj):
        """Display formatted price with currency."""
        return format_html('<strong>₹{}</strong> / {}', obj.price, obj.billing_cycle)


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    """Admin interface for Organization Subscriptions."""
    
    list_display = ("id", "organization", "plan", "status", "starts_at", "ends_at", "assigned_by", "created_at")
    list_filter = ("status", "plan", "starts_at", "ends_at", "created_at")
    search_fields = ("organization__name", "plan__name", "plan__code", "assigned_by__email")
    readonly_fields = ("created_at", "updated_at")
    date_hierarchy = "starts_at"
    list_per_page = 50
    autocomplete_fields = ("organization", "plan", "assigned_by")
    
    fieldsets = (
        ("Subscription Details", {
            "fields": ("organization", "plan", "status")
        }),
        ("Duration", {
            "fields": ("starts_at", "ends_at")
        }),
        ("Metadata", {
            "fields": ("assigned_by", "notes", "created_at", "updated_at"),
            "classes": ("collapse",)
        }),
    )
    
    def get_queryset(self, request):
        """Optimize queryset with select_related."""
        qs = super().get_queryset(request)
        return qs.select_related("organization", "plan", "assigned_by")
from django.contrib import admin
from .models import Plan, Subscription


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ("id", "code", "name", "max_users", "billing_cycle", "price", "created_at")
    search_fields = ("code", "name")


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("id", "organization", "plan", "status", "starts_at", "ends_at", "assigned_by")
    list_filter = ("status", "plan")
from django.contrib import admin
from .models import Organization, OrgMembership, OrganizationAPIKey, Module, OrganizationModule


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "slug", "status", "owner", "created_by", "created_at")
    search_fields = ("name", "slug")
    list_filter = ("status",)


@admin.register(OrgMembership)
class OrgMembershipAdmin(admin.ModelAdmin):
    list_display = ("id", "organization", "user", "role", "is_active", "created_at")
    list_filter = ("role", "is_active")
    search_fields = ("organization__name", "user__email")


@admin.register(OrganizationAPIKey)
class OrganizationAPIKeyAdmin(admin.ModelAdmin):
    list_display = ("id", "organization", "name", "created_by", "created_at")
    search_fields = ("organization__name", "name")


@admin.register(Module)
class ModuleAdmin(admin.ModelAdmin):
    list_display = ("id", "code", "name", "created_at")
    search_fields = ("code", "name")
    ordering = ("code",)


@admin.register(OrganizationModule)
class OrganizationModuleAdmin(admin.ModelAdmin):
    list_display = ("id", "organization", "module", "is_enabled", "created_at")
    list_filter = ("is_enabled", "module")
    search_fields = ("organization__name", "module__code", "module__name")

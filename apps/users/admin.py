from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.translation import gettext_lazy as _
from django.contrib import messages
from django.http import HttpResponseRedirect
from django.urls import path

User = get_user_model()


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    """Enhanced admin interface for the custom User model."""

    list_display = ("id", "email", "first_name", "last_name", "is_active", "is_staff", "is_superuser", "date_joined")
    search_fields = ("email", "first_name", "last_name")
    list_filter = ("is_active", "is_staff", "is_superuser", "date_joined")
    ordering = ("-date_joined",)
    readonly_fields = ("date_joined", "last_login")
    actions = ["reset_user_passwords"]

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        (_("Personal info"), {"fields": ("first_name", "last_name")}),
        (_("Permissions"), {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        (_("Important dates"), {"fields": ("last_login", "date_joined")}),
    )

    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("email", "password1", "password2", "first_name", "last_name", "is_active", "is_staff")
        }),
    )

    def reset_user_passwords(self, request, queryset):
        """Reset selected users' passwords to a default value."""
        default_password = "Welcome@123"
        count = 0
        for user in queryset:
            user.set_password(default_password)
            user.save()
            count += 1

        self.message_user(
            request,
            f"Successfully reset passwords for {count} users to: {default_password}",
            messages.SUCCESS
        )
    reset_user_passwords.short_description = "Reset selected users' passwords to default"

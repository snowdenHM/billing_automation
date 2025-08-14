from django.contrib import admin
from django.contrib.auth import get_user_model

User = get_user_model()


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ("id", "email", "first_name", "last_name", "is_active", "is_staff", "is_superuser")
    search_fields = ("email", "first_name", "last_name")
    list_filter = ("is_active", "is_staff", "is_superuser")
    ordering = ("-id",)
from django.contrib import admin
from unfold.admin import ModelAdmin as UnfoldModelAdmin, TabularInline as UnfoldTabularInline

from .models import SupportTicket, SupportTicketMessage, SupportTicketRecipient


class SupportTicketMessageInline(UnfoldTabularInline):
    model = SupportTicketMessage
    extra = 0
    fields = ("author_user", "author_email", "body", "is_internal", "created_at")
    readonly_fields = ("created_at",)


@admin.register(SupportTicket)
class SupportTicketAdmin(UnfoldModelAdmin):
    list_display = (
        "subject",
        "category",
        "status",
        "priority",
        "organization",
        "user",
        "created_at",
    )
    list_filter = ("status", "category", "priority", "is_deleted")
    search_fields = ("subject", "message", "user__email", "organization__name")
    readonly_fields = ("id", "created_at", "updated_at")
    inlines = [SupportTicketMessageInline]


@admin.register(SupportTicketMessage)
class SupportTicketMessageAdmin(UnfoldModelAdmin):
    list_display = ("ticket", "author_user", "author_email", "is_internal", "created_at")
    list_filter = ("is_internal",)
    search_fields = ("body", "author_email", "ticket__subject")
    readonly_fields = ("id", "created_at")


@admin.register(SupportTicketRecipient)
class SupportTicketRecipientAdmin(UnfoldModelAdmin):
    list_display = ("email", "name", "receive_new", "receive_reply", "created_at")
    list_filter = ("receive_new", "receive_reply")
    search_fields = ("email", "name")

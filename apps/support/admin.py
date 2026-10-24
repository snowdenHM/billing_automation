from django.contrib import admin
from unfold.admin import ModelAdmin as UnfoldModelAdmin, TabularInline as UnfoldTabularInline

from apps.common.admin import BaseOrgAdmin, SuperuserOnlyAdmin

from .models import SupportTicket, SupportTicketMessage, SupportTicketRecipient


class SupportTicketMessageInline(UnfoldTabularInline):
    model = SupportTicketMessage
    extra = 0
    fields = ("author_user", "author_email", "body", "is_internal", "created_at")
    readonly_fields = ("created_at",)


@admin.register(SupportTicket)
class SupportTicketAdmin(BaseOrgAdmin):
    list_display = (
        "subject",
        "category",
        "status",
        "priority",
        "source",
        "contact_email",
        "organization",
        "user",
        "created_at",
    )
    # list_display shows organization and user, so join them instead of
    # letting Django fire two extra queries for every row on the page.
    list_select_related = ("organization", "user")
    list_filter = ("status", "category", "priority", "source", "is_deleted")
    search_fields = (
        "subject", "message", "user__email", "organization__name",
        "contact_name", "contact_email",
    )
    readonly_fields = ("id", "created_at", "updated_at")
    inlines = [SupportTicketMessageInline]


@admin.register(SupportTicketMessage)
class SupportTicketMessageAdmin(BaseOrgAdmin):
    # A message has no organisation of its own; it inherits the ticket's.
    # Without this the messages list was the way around the scoped ticket
    # list — the bodies are right there in list_display.
    org_lookup = "ticket__organization"

    list_display = ("ticket", "author_user", "author_email", "is_internal", "created_at")
    list_select_related = ("ticket", "author_user")
    list_filter = ("is_internal",)
    search_fields = ("body", "author_email", "ticket__subject")
    readonly_fields = ("id", "created_at")


@admin.register(SupportTicketRecipient)
class SupportTicketRecipientAdmin(SuperuserOnlyAdmin):
    # Who gets notified about *any* ticket on this deployment — an internal
    # ops list, not a tenant's data, so there is no org to scope it by.
    list_display = ("email", "name", "receive_new", "receive_reply", "created_at")
    list_filter = ("receive_new", "receive_reply")
    search_fields = ("email", "name")

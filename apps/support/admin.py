from django.contrib import admin
from unfold.admin import ModelAdmin as UnfoldModelAdmin, TabularInline as UnfoldTabularInline

from apps.common.admin import BaseOrgAdmin, SuperuserOnlyAdmin

from .models import (
    SupportAccessEvent,
    SupportAccessGrant,
    SupportAccessSession,
    SupportAgent,
    SupportTicket,
    SupportTicketMessage,
    SupportTicketRecipient,
)


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


# ---------------------------------------------------------------------------
# Consent-gated access
# ---------------------------------------------------------------------------

@admin.register(SupportAgent)
class SupportAgentAdmin(SuperuserOnlyAdmin):
    # Who may look into client accounts at all. Superuser-only because
    # adding a row here is the second half of becoming a support agent —
    # if any staff account could add one, the two-key design is one key.
    list_display = ("display_name", "user", "is_active", "max_open_tickets", "created_at")
    list_select_related = ("user",)
    list_filter = ("is_active",)
    search_fields = ("display_name", "user__email")
    readonly_fields = ("created_at",)


@admin.register(SupportAccessGrant)
class SupportAccessGrantAdmin(BaseOrgAdmin):
    list_display = (
        "code_label", "organization", "ticket", "scope", "status",
        "expires_at", "redeemed_by", "created_at",
    )
    list_select_related = ("organization", "ticket", "redeemed_by")
    list_filter = ("status", "scope")
    search_fields = ("code_label", "organization__name", "ticket__subject")
    # Every field: a grant is a record of consent, and consent is not
    # editable after the fact. Revocation goes through the API (or the
    # ticket closing), both of which end the live sessions as well —
    # editing ``status`` here would shut the record and leave the door open.
    # ``token_hash`` is excluded entirely rather than shown read-only:
    # nobody has a reason to read it off a screen.
    readonly_fields = (
        "id", "ticket", "organization", "created_by", "code_label", "scope",
        "expires_at", "status", "redeemed_at", "redeemed_by", "revoked_at",
        "revoked_by", "revoke_reason", "created_at",
    )
    exclude = ("token_hash",)

    def has_add_permission(self, request):
        # Only the client mints. No admin screen creates consent.
        return False


@admin.register(SupportAccessSession)
class SupportAccessSessionAdmin(BaseOrgAdmin):
    org_lookup = "grant__organization"

    list_display = ("id", "grant", "agent", "started_at", "ended_at", "end_reason", "ip")
    list_select_related = ("grant", "agent")
    list_filter = ("end_reason",)
    search_fields = ("agent__email", "grant__code_label")
    readonly_fields = ("id", "grant", "agent", "started_at", "ended_at", "end_reason", "ip", "user_agent")

    def has_add_permission(self, request):
        return False


@admin.register(SupportAccessEvent)
class SupportAccessEventAdmin(BaseOrgAdmin):
    org_lookup = "session__grant__organization"

    list_display = ("at", "session", "method", "path", "status_code")
    list_select_related = ("session",)
    list_filter = ("method",)
    search_fields = ("path", "session__agent__email")
    readonly_fields = ("session", "method", "path", "status_code", "at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        # An audit trail that can be edited is not one.
        return False

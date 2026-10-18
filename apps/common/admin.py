"""
Shared admin base classes and mixins.

All org-scoped ``ModelAdmin`` classes should inherit from
:class:`BaseOrgAdmin` instead of ``admin.ModelAdmin`` directly.
"""

from django.contrib import admin, messages
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from unfold.admin import (
    ModelAdmin as UnfoldModelAdmin,
    TabularInline as UnfoldTabularInline,
    StackedInline as UnfoldStackedInline,
)

from apps.common.models import DemoRequest, EmailSettings


def _user_org_ids(user):
    """Ids of the organisations *user* is an active member of."""
    from apps.organizations.models import OrgMembership

    return OrgMembership.objects.filter(
        user=user, is_active=True
    ).values_list("organization_id", flat=True)



@admin.register(DemoRequest)
class DemoRequestAdmin(UnfoldModelAdmin):
    """Read-mostly view of public demo bookings."""

    list_display = (
        "full_name", "organization", "email", "phone",
        "accounting_software", "email_verified", "status", "created_at",
    )
    list_filter = ("status", "email_verified", "accounting_software", "created_at")
    search_fields = ("full_name", "organization", "email", "phone")
    list_editable = ("status",)
    readonly_fields = (
        "id", "full_name", "organization", "accounting_software",
        "email", "phone", "email_verified", "email_verified_at",
        "created_at", "updated_at",
    )
    fieldsets = (
        ("Contact", {
            "fields": (
                "full_name", "organization", "email", "email_verified",
                "email_verified_at", "phone", "accounting_software",
            ),
        }),
        ("Follow-up", {
            "fields": ("status", "notes"),
        }),
        ("Metadata", {
            "fields": ("id", "created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        # Bookings only ever arrive through the public form.
        return False


@admin.register(EmailSettings)
class EmailSettingsAdmin(UnfoldModelAdmin):
    """Singleton admin — always edit the same row (pk=1)."""

    # "id" first meant the only link was a bare number; from_email is the
    # column that identifies the row.
    list_display = ("from_email", "from_name", "provider", "is_enabled", "updated_at")
    list_display_links = ("from_email",)
    fieldsets = (
        (None, {
            "fields": ("is_enabled", "provider"),
            "description": (
                "<b>Provider</b> decides which service actually carries the "
                "mail. Change it here to move off a failing provider without "
                "a redeploy — only the section matching your choice is used, "
                "the others are ignored."
            ),
        }),
        ("SMTP", {
            "fields": (
                "smtp_host", "smtp_port", "smtp_username", "smtp_password",
                "smtp_use_tls", "smtp_use_ssl",
            ),
            "description": (
                "Used when Provider is <b>SMTP</b>. Works with Brevo, Mailgun, "
                "Postmark, Amazon SES, Gmail and anything else that speaks "
                "SMTP.<br>"
                "Port <code>587</code> with <b>STARTTLS</b> ticked is the "
                "usual setup; port <code>465</code> needs <b>implicit SSL</b> "
                "instead. Do not tick both."
            ),
        }),
        ("SendGrid", {
            "fields": ("sendgrid_api_key",),
            "classes": ("collapse",),
            "description": (
                "Used when Provider is <b>SendGrid</b>. Paste the API key "
                "(starts with <code>SG.</code>). The sender address below "
                "must be a verified single sender or belong to an "
                "authenticated domain in that SendGrid account.<br>"
                "Note: django-anymail has dropped official SendGrid support, "
                "so SMTP is the better choice for new setups."
            ),
        }),
        ("Sender identity", {
            "fields": ("from_email", "from_name", "reply_to"),
            "description": (
                "Applies to every provider. The address must be one your "
                "provider has authorised to send, or mail will be rejected."
            ),
        }),
    )
    readonly_fields = ("updated_at",)

    def has_add_permission(self, request):
        # Enforce singleton: only allow the row to be created if it
        # doesn't exist yet.
        return not EmailSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        # Redirect the list page straight to the single row's edit page.
        obj = EmailSettings.load()
        from django.shortcuts import redirect
        return redirect(
            "admin:{app}_{model}_change".format(
                app=self.opts.app_label,
                model=self.opts.model_name,
            ),
            obj.pk,
        )

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        messages.success(
            request,
            "Email settings saved. Cache invalidated — the next outbound "
            "email will use the new credentials.",
        )


# ---------------------------------------------------------------------------
# URL helper (moved from zoho/admin.py)
# ---------------------------------------------------------------------------

def admin_change_url(obj):
    """Return the admin change-page URL for any model instance."""
    return reverse(
        f"admin:{obj._meta.app_label}_{obj._meta.model_name}_change",
        args=[obj.pk],
    )


# ---------------------------------------------------------------------------
# Organisation-scoped base
# ---------------------------------------------------------------------------

class BaseOrgAdmin(UnfoldModelAdmin):
    """
    Shared base for any ``ModelAdmin`` whose model has an
    ``organization`` FK.

    * Scopes the queryset to the current user's organisations.
    * Auto-fills ``organization`` on save when missing.
    * Auto-adds ``created_at`` / ``updated_at`` / ``update_at`` to
      ``readonly_fields``.
    * Restricts the ``organization`` FK dropdown to the user's orgs.
    """

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if hasattr(self.model, "organization"):
            qs = qs.select_related("organization")
        if request.user.is_superuser:
            return qs
        # Scope to the orgs the user is an active member of. The previous
        # version probed ``request.user.organizations``, which is not a
        # relation on User, so it fell through and returned every tenant's
        # rows to any staff user.
        if hasattr(self.model, "organization"):
            return qs.filter(organization__in=_user_org_ids(request.user))
        return qs

    def save_model(self, request, obj, form, change):
        if hasattr(obj, "organization_id") and not obj.organization_id:
            org_id = getattr(request.user, "organization_id", None)
            if org_id:
                obj.organization_id = org_id
        super().save_model(request, obj, form, change)

    def get_readonly_fields(self, request, obj=None):
        ro = list(super().get_readonly_fields(request, obj))
        model_field_names = {f.name for f in self.model._meta.get_fields()}
        for f in ("created_at", "updated_at", "update_at"):
            if f in model_field_names and f not in ro:
                ro.append(f)
        return ro

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "organization" and not request.user.is_superuser:
            from apps.organizations.models import Organization

            kwargs["queryset"] = Organization.objects.filter(
                id__in=_user_org_ids(request.user)
            )
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


# ---------------------------------------------------------------------------
# Reusable display mixins
# ---------------------------------------------------------------------------

class FileDisplayMixin:
    """Adds a ``display_file`` column that links to the bill file."""

    @admin.display(description="File", ordering="file")
    def display_file(self, obj):
        if getattr(obj, "file", None):
            return format_html(
                '<a href="{}" target="_blank">View File</a>', obj.file.url,
            )
        return "-"


class OwnershipDisplayMixin:
    """Adds a ``display_ownership`` column for ``bill_belong_your_org``."""

    @admin.display(
        description="Ownership",
        ordering="bill_belong_your_org",
    )
    def display_ownership(self, obj):
        if obj.bill_belong_your_org:
            return format_html('<span style="color: green;">✓ Own Bill</span>')
        return format_html('<span style="color: orange;">⚬ Vendor Bill</span>')


class FKLinkMixin:
    """
    Utility to render an FK as a clickable admin link.

    Usage in a subclass::

        @admin.display(description="Selected Bill")
        def selectBill_link(self, obj):
            return self._fk_link(obj, "selectBill", label_attr="billmunshiName")
    """

    @staticmethod
    def _fk_link(obj, fk_name, *, label_attr=None):
        fk_id = getattr(obj, f"{fk_name}_id", None)
        if not fk_id:
            return "-"
        related = getattr(obj, fk_name)
        url = admin_change_url(related)
        label = getattr(related, label_attr, None) if label_attr else None
        label = label or str(fk_id)
        return mark_safe(f'<a href="{url}">{label}</a>')

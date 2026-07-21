"""
Shared admin base classes and mixins.

All org-scoped ``ModelAdmin`` classes should inherit from
:class:`BaseOrgAdmin` instead of ``admin.ModelAdmin`` directly.
"""

from django.contrib import admin, messages
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from apps.common.models import EmailSettings


@admin.register(EmailSettings)
class EmailSettingsAdmin(admin.ModelAdmin):
    """Singleton admin — always edit the same row (pk=1)."""

    list_display = ("id", "is_enabled", "from_email", "from_name", "updated_at")
    fieldsets = (
        (None, {
            "fields": ("is_enabled",),
        }),
        ("SendGrid", {
            "fields": ("sendgrid_api_key",),
            "description": (
                "Paste the SendGrid API key here (starts with <code>SG.</code>). "
                "The sender address below must be a verified single sender "
                "or belong to an authenticated domain in your SendGrid account."
            ),
        }),
        ("Sender identity", {
            "fields": ("from_email", "from_name", "reply_to"),
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

class BaseOrgAdmin(admin.ModelAdmin):
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
        # Many-to-many via OrgMembership (preferred)
        if hasattr(request.user, "organizations"):
            return qs.filter(organization__in=request.user.organizations.all())
        # Single FK fallback
        org_id = getattr(request.user, "organization_id", None)
        if org_id and hasattr(self.model, "organization_id"):
            return qs.filter(organization_id=org_id)
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
            if hasattr(request.user, "organizations"):
                kwargs["queryset"] = request.user.organizations.all()
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

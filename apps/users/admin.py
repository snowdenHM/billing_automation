import os
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.translation import gettext_lazy as _
from django.contrib import messages
from django.http import HttpResponseRedirect
from django.urls import path

from unfold.admin import ModelAdmin as UnfoldModelAdmin
from unfold.forms import AdminPasswordChangeForm, UserChangeForm, UserCreationForm

User = get_user_model()


@admin.register(User)
class UserAdmin(BaseUserAdmin, UnfoldModelAdmin):
    """Enhanced admin interface for the custom User model."""

    form = UserChangeForm
    add_form = UserCreationForm
    change_password_form = AdminPasswordChangeForm

    # Django links only the FIRST column to the change page. This led with
    # "id", so the sole way into a user record was a two-character integer
    # — operators clicked the email (the identifier they actually think in)
    # and nothing happened, which read as "the detail page is missing".
    list_display = ("email", "first_name", "last_name", "is_active", "is_staff", "is_superuser", "date_joined")
    list_display_links = ("email",)
    search_fields = ("email", "first_name", "last_name", "signup_organization_name", "signup_organization_gst")
    list_filter = ("is_active", "is_staff", "is_superuser", "email_verified", "date_joined")
    ordering = ("-date_joined",)
    readonly_fields = ("date_joined", "last_login")
    actions = ["reset_user_passwords"]
    date_hierarchy = "date_joined"
    list_per_page = 50

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        (_("Personal info"), {"fields": ("first_name", "last_name", "phone_number")}),
        (_("Email verification"), {"fields": ("email_verified", "email_verification_required")}),
        (_("Signup details"), {"fields": (
            "signup_organization_name", "signup_organization_gst",
            "signup_designation", "signup_accounting_software",
        )}),
        (_("Permissions"), {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        (_("Important dates"), {"fields": ("last_login", "date_joined")}),
    )

    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("email", "password1", "password2", "first_name", "last_name", "is_active", "is_staff")
        }),
    )

    def get_queryset(self, request):
        """Only the users a non-superuser staff member shares a workspace with.

        Unscoped, this listed every account on the deployment: a staff
        user at one client could read another client's people, and with
        the change-user permission could edit them. Scoped through
        OrgMembership, a staff member sees the people they already work
        with and nobody else.

        Superusers keep the full list — they are the ones who have to
        find an account across tenants when something goes wrong.
        """
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        from apps.common.admin import _user_org_ids
        from apps.organizations.models import OrgMembership

        peers = OrgMembership.objects.filter(
            organization_id__in=_user_org_ids(request.user), is_active=True,
        ).values_list("user_id", flat=True)
        # Their own row stays reachable even before any membership exists.
        return qs.filter(pk__in=list(peers) + [request.user.pk])

    def get_fieldsets(self, request, obj=None):
        """Hide the Permissions block from anyone who is not a superuser.

        It carries ``is_superuser``, ``is_staff``, ``groups`` and
        ``user_permissions``. A staff user who can edit a user and see
        that block can tick ``is_superuser`` on their own row, which
        turns limited admin access into full control of every tenant.
        Scoping the list above does not help, because their own row is
        deliberately in it.
        """
        fieldsets = super().get_fieldsets(request, obj)
        if request.user.is_superuser:
            return fieldsets
        return tuple(
            (name, opts) for name, opts in fieldsets
            if name != _("Permissions")
        )

    def get_readonly_fields(self, request, obj=None):
        """Belt and braces for the fields the block above hides.

        A hidden fieldset is not an access control on its own: a crafted
        POST can still carry the field. Marking them read-only makes the
        admin drop whatever arrives for them.
        """
        fields = tuple(super().get_readonly_fields(request, obj))
        if request.user.is_superuser:
            return fields
        return fields + ("is_staff", "is_superuser", "groups", "user_permissions")

    def has_delete_permission(self, request, obj=None):
        """A staff user must not delete accounts outside their own tenant.

        The queryset scope stops them seeing such a row, but a delete by
        primary key does not go through the changelist.
        """
        if request.user.is_superuser:
            return super().has_delete_permission(request, obj)
        if obj is None:
            return super().has_delete_permission(request, obj)
        return self.get_queryset(request).filter(pk=obj.pk).exists()

    def get_actions(self, request):
        actions = super().get_actions(request)
        # This action sets every selected account to one password read from
        # the environment (default "ChangeMeNow!1"). That is an account
        # takeover primitive, not a maintenance convenience: anyone with
        # staff access and the change-user permission could point it at a
        # superuser. Only superusers get to see it.
        if not request.user.is_superuser:
            actions.pop("reset_user_passwords", None)
        return actions

    @admin.action(
        description="Reset selected users' passwords to default",
        permissions=["superuser"],
    )
    def reset_user_passwords(self, request, queryset):
        """Reset selected users' passwords to a default value."""
        default_password = os.environ.get('DEFAULT_RESET_PASSWORD', 'ChangeMeNow!1')
        count = 0
        for user in queryset:
            user.set_password(default_password)
            user.save()
            count += 1

        self.message_user(
            request,
            f"Successfully reset passwords for {count} users.",
            messages.SUCCESS
        )
    def has_superuser_permission(self, request):
        """Backs ``permissions=["superuser"]`` on the action above."""
        return request.user.is_superuser

"""
Shared permission classes used across the project.
"""
import logging

from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission, SAFE_METHODS, IsAuthenticated

from apps.organizations.models import OrgMembership

logger = logging.getLogger(__name__)


def assert_org_access(user, organization, *, admin_only: bool = False) -> None:
    """Raise PermissionDenied unless *user* may access *organization*.

    Centralizes the previously-duplicated `is_staff OR membership.exists()`
    pattern that lived in every organizations view.

    - Superusers and staff always pass.
    - Otherwise the user must have an active OrgMembership.
    - When ``admin_only`` is set, the membership must have the ADMIN role.
    """
    if not user or not user.is_authenticated:
        raise PermissionDenied("Authentication required.")
    if user.is_superuser or user.is_staff:
        return
    qs = organization.memberships.filter(user=user, is_active=True)
    if admin_only:
        qs = qs.filter(role=OrgMembership.ADMIN)
    if not qs.exists():
        raise PermissionDenied(
            "You don't have permission to access this organization."
            if not admin_only
            else "You must be an organization admin to perform this action."
        )


class IsSuperAdmin(BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_superuser)


class IsSelfOrAdmin(BasePermission):
    def has_object_permission(self, request, view, obj):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.user.is_superuser:
            return True
        return getattr(obj, "id", None) == getattr(request.user, "id", None)

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated)


class IsOrgAdmin(BasePermission):
    """
    Permission class to check that the user is an admin of the SPECIFIC
    organization referenced in the URL (``org_id`` view kwarg).

    Previously this checked "is admin of any organization" — an admin of
    Org A could pass this permission even on Org B's endpoints, defeating
    per-org isolation. The IDOR guard in
    ``get_organization_from_request`` closes the read hole, but for
    write endpoints (upload, verify, sync, etc.) this permission is the
    first line of defence and must be scoped to the URL org.

    Fallback: if no ``org_id`` kwarg is present (endpoints not scoped by
    URL), keep the old "any-org admin" behaviour so shared
    account-wide endpoints don't accidentally break.
    """
    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if user.is_superuser or user.is_staff:
            return True

        org_id = None
        if hasattr(view, "kwargs") and isinstance(view.kwargs, dict):
            org_id = view.kwargs.get("org_id")

        base_qs = OrgMembership.objects.filter(
            user=user,
            role=OrgMembership.ADMIN,
            is_active=True,
        )
        if org_id:
            return base_qs.filter(organization_id=org_id).exists()
        # No org_id in the URL — retain the wider "admin of any org"
        # semantic. Views that need per-org enforcement without an
        # ``org_id`` kwarg should call ``assert_org_access(admin_only=True)``
        # directly.
        return base_qs.exists()

    def has_object_permission(self, request, view, obj):
        # ``obj`` may be a bill or nested resource carrying ``.organization``.
        # When it does, verify the admin role against THAT org — this is
        # the strictest form and closes the IDOR loop on detail views too.
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if user.is_superuser or user.is_staff:
            return True
        org = getattr(obj, "organization", None) or getattr(obj, "organization_id", None)
        if org:
            org_id = getattr(org, "id", org)
            return OrgMembership.objects.filter(
                user=user,
                role=OrgMembership.ADMIN,
                is_active=True,
                organization_id=org_id,
            ).exists()
        return self.has_permission(request, view)


class OrganizationAPIKeyOrBearerToken(BasePermission):
    """
    Allow access via Organization API key OR Bearer token authentication.
    This is an OR condition between authentication methods.

    When an API key is used, ``request.organization`` is set to the
    associated Organization instance.
    """

    def has_permission(self, request, view):
        auth_header = request.META.get("HTTP_AUTHORIZATION", "")

        # --- API Key path ---
        if auth_header.startswith("Api-Key "):
            api_key_value = auth_header.replace("Api-Key ", "", 1)
            from rest_framework_api_key.models import APIKey
            from apps.organizations.models import OrganizationAPIKey

            try:
                api_key_obj = APIKey.objects.get_from_key(api_key_value)
                if api_key_obj:
                    org_api_key = OrganizationAPIKey.objects.get(api_key=api_key_obj)
                    request.organization = org_api_key.organization
                    return True
            except (APIKey.DoesNotExist, OrganizationAPIKey.DoesNotExist):
                pass
            except Exception as exc:
                logger.error("API Key validation error: %s", exc)

        # --- Bearer token path ---
        if IsAuthenticated().has_permission(request, view):
            return IsOrgAdmin().has_permission(request, view)

        return False

"""
Shared permission classes used across the project.
"""
import logging

from rest_framework.permissions import BasePermission, SAFE_METHODS, IsAuthenticated

from apps.organizations.models import OrgMembership

logger = logging.getLogger(__name__)


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
    Permission class to check if the user is an admin of any organization.
    For object-level organization checks, use IsOrgAdminForObject from organizations.permissions instead.
    """
    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        return OrgMembership.objects.filter(
            user=user,
            role=OrgMembership.ADMIN,
            is_active=True
        ).exists()

    def has_object_permission(self, request, view, obj):
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

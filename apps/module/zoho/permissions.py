# apps/zoho/permissions.py

from typing import Optional
from uuid import UUID

from django.utils.functional import cached_property
from rest_framework.permissions import BasePermission
from waffle import switch_is_active
from rest_framework_api_key.models import APIKey

from apps.organizations.models import OrgMembership, OrganizationAPIKey


def _get_org_id_from_view(view):
    """
    Pull org_id from the view/kwargs consistently.
    """
    # Views in this app use path("zoho/<uuid:org_id>/..."), so kwargs should have it.
    try:
        return view.kwargs.get("org_id")
    except (AttributeError, KeyError):
        return None


def _waffle_switch_name(org_id) -> str:
    """
    Generate waffle switch name for organization module.
    Works with both UUID and legacy integer IDs.
    """
    return f"org:{org_id}:zoho"


class IsOrgAdminOrOrgAPIKey(BasePermission):
    """
    Allow if EITHER:
      • request.user is a superuser, OR
      • request.user is an ACTIVE ADMIN member of the organization, OR
      • request is authenticated with a valid Organization API key for that org
        via header:  Authorization: Api-Key <key>
    """

    message = "Requires organization admin privileges or a valid Organization API key."

    def has_permission(self, request, view) -> bool:
        org_id = _get_org_id_from_view(view)
        if not org_id:
            return False

        # 1) Superuser short-circuit
        user = getattr(request, "user", None)
        if user and getattr(user, "is_authenticated", False) and getattr(user, "is_superuser", False):
            return True

        # 2) Org admin?
        if user and getattr(user, "is_authenticated", False):
            is_admin = OrgMembership.objects.filter(
                organization_id=org_id,
                user=user,
                role=OrgMembership.ADMIN,
                is_active=True,
            ).exists()
            if is_admin:
                return True

        # 3) Organization API key?
        # Expect header: "Authorization: Api-Key <raw_key>"
        auth = request.META.get("HTTP_AUTHORIZATION", "")
        if auth.startswith("Api-Key "):
            raw_key = auth.split(" ", 1)[1].strip()
            if raw_key:
                # Validate & resolve to APIKey instance using djangorestframework-api-key helper
                try:
                    api_key_obj = APIKey.objects.get_from_key(raw_key)
                except Exception:
                    return False

                return OrganizationAPIKey.objects.filter(
                    organization_id=org_id,
                    api_key=api_key_obj,
                    api_key__revoked=False,
                ).exists()

        return False


class ModuleZohoEnabled(BasePermission):
    """
    Gate every Zoho endpoint behind a waffle switch:
        name = "org:<org_id>:zoho"
    Also checks OrganizationModule table as fallback.
    """

    message = "Zoho module is not enabled for this organization."

    def has_permission(self, request, view) -> bool:
        org_id = _get_org_id_from_view(view)
        if not org_id:
            return False

        # First try waffle switch
        if switch_is_active(_waffle_switch_name(org_id)):
            return True

        # Fallback: check OrganizationModule directly
        from apps.organizations.models import OrganizationModule

        return OrganizationModule.objects.filter(
            organization_id=org_id, module__code="zoho", is_active=True
        ).exists()

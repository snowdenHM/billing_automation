from rest_framework.permissions import BasePermission
from .models import OrgMembership, Organization


class IsOrgAdminForObject(BasePermission):
    """Allows access only to org admins of the targeted object (Organization or nested resources)."""

    def has_object_permission(self, request, view, obj):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        org = obj if isinstance(obj, Organization) else getattr(obj, "organization", None)
        if not org:
            return False
        return OrgMembership.objects.filter(user=user, organization=org, role=OrgMembership.ADMIN, is_active=True).exists()

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated)
from rest_framework.permissions import BasePermission, SAFE_METHODS
from apps.organizations.models import OrgMembership


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

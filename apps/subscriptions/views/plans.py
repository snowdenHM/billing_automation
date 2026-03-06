# apps/subscriptions/views/plans.py
"""Plan management ViewSet."""

from drf_spectacular.utils import extend_schema
from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from apps.common.permissions import IsSuperAdmin
from ..models import Plan
from ..serializers import PlanSerializer


@extend_schema(tags=["Plans & Subscriptions"])
class PlanViewSet(viewsets.ModelViewSet):
    """
    API endpoint for subscription plans management.

    Only admins can create/update/delete plans, but all authenticated users can view them.
    """
    serializer_class = PlanSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        """Filter plans based on user role."""
        if not self.request.user.is_staff:
            return Plan.objects.filter(is_active=True).order_by("price", "code")
        return Plan.objects.all().order_by("price", "code")

    def get_permissions(self):
        if self.action in ["create", "update", "partial_update", "destroy"]:
            return [IsSuperAdmin()]
        return super().get_permissions()

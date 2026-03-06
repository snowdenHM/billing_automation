# apps/subscriptions/views/subscriptions.py
"""Subscription management ViewSet."""

from drf_spectacular.utils import extend_schema, OpenApiParameter
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.permissions import IsSuperAdmin
from ..models import Subscription
from ..serializers import (
    SubscriptionSerializer,
    SubscriptionCancelSerializer,
    SubscriptionRenewSerializer,
    SubscriptionChangePlanSerializer,
)


@extend_schema(
    tags=["Plans & Subscriptions"],
    parameters=[
        OpenApiParameter(name="id", type=str, location=OpenApiParameter.PATH, description="Subscription UUID")
    ],
)
class SubscriptionViewSet(viewsets.ModelViewSet):
    """API endpoint for managing organization subscriptions."""
    serializer_class = SubscriptionSerializer
    permission_classes = [IsAuthenticated]
    queryset = Subscription.objects.none()  # Base queryset for schema generation

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        return Subscription.objects.filter(
            organization__memberships__user=self.request.user,
            organization__memberships__is_active=True,
        ).select_related("plan", "organization")

    def get_permissions(self):
        if self.action in ["create", "update", "partial_update", "destroy"]:
            return [IsSuperAdmin()]
        return super().get_permissions()

    @extend_schema(request=SubscriptionCancelSerializer, responses={200: SubscriptionSerializer})
    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        """Cancel a subscription but maintain access until the end date."""
        subscription = self.get_object()

        if not (
            request.user.is_staff
            or subscription.organization.memberships.filter(
                user=request.user, role="ADMIN", is_active=True
            ).exists()
        ):
            return Response(
                {"detail": "You don't have permission to cancel this subscription."},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = SubscriptionCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        reason = serializer.validated_data.get("reason")
        if reason:
            subscription.notes = f"{subscription.notes}\n\nCancellation reason: {reason}".strip()

        subscription.cancel()
        return Response(SubscriptionSerializer(subscription).data)

    @extend_schema(request=SubscriptionRenewSerializer, responses={200: SubscriptionSerializer})
    @action(detail=True, methods=["post"], permission_classes=[IsSuperAdmin])
    def renew(self, request, pk=None):
        """Renew a subscription for another period."""
        subscription = self.get_object()
        serializer = SubscriptionRenewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        days = serializer.validated_data.get("days")
        subscription.renew(days)
        return Response(SubscriptionSerializer(subscription).data)

    @extend_schema(request=SubscriptionChangePlanSerializer, responses={200: SubscriptionSerializer})
    @action(detail=True, methods=["post"], permission_classes=[IsSuperAdmin])
    def change_plan(self, request, pk=None):
        """Change the subscription to a different plan."""
        subscription = self.get_object()
        serializer = SubscriptionChangePlanSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        new_plan = serializer.validated_data["plan"]
        subscription.change_plan(new_plan)
        return Response(SubscriptionSerializer(subscription).data)

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from django.utils import timezone
from django.shortcuts import get_object_or_404

from drf_spectacular.utils import extend_schema, OpenApiParameter
from apps.common.permissions import IsSuperAdmin, IsOrgAdmin
from apps.organizations.models import Organization

from .models import Plan, Subscription
from .serializers import (
    PlanSerializer,
    SubscriptionSerializer,
    SubscriptionCancelSerializer,
    SubscriptionRenewSerializer,
    SubscriptionChangePlanSerializer
)


@extend_schema(tags=["Plans"])
class PlanViewSet(viewsets.ModelViewSet):
    """
    API endpoint for subscription plans management.

    Only admins can create/update/delete plans, but all authenticated users can view them.
    """
    serializer_class = PlanSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        """Filter plans based on user role"""
        # For regular users, only show active plans
        if not self.request.user.is_staff:
            return Plan.objects.filter(is_active=True).order_by('price', 'code')
        # For admins, show all plans
        return Plan.objects.all().order_by('price', 'code')

    def get_permissions(self):
        """
        Only super admin can create/update/delete plans,
        but all authenticated users can view them.
        """
        if self.action in ["create", "update", "partial_update", "destroy"]:
            return [IsSuperAdmin()]
        return super().get_permissions()


@extend_schema(
    tags=["Subscriptions"],
    parameters=[
        OpenApiParameter(
            name="id",
            type=str,
            location=OpenApiParameter.PATH,
            description="Subscription UUID"
        )
    ]
)
class SubscriptionViewSet(viewsets.ModelViewSet):
    """API endpoint for managing organization subscriptions."""
    serializer_class = SubscriptionSerializer
    permission_classes = [IsAuthenticated]
    queryset = Subscription.objects.none()  # Base queryset for schema generation

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):  # for schema generation
            return self.queryset
        return Subscription.objects.filter(
            organization__memberships__user=self.request.user,
            organization__memberships__is_active=True
        ).select_related('plan', 'organization')

    def get_permissions(self):
        """
        Only super admin can create/update/delete subscriptions,
        but org admins can view their own organization's subscription.
        """
        if self.action in ["create", "update", "partial_update", "destroy"]:
            return [IsSuperAdmin()]
        return super().get_permissions()

    @extend_schema(
        request=SubscriptionCancelSerializer,
        responses={200: SubscriptionSerializer}
    )
    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Cancel a subscription but maintain access until the end date"""
        subscription = self.get_object()

        # Only super admins or organization admins can cancel
        if not (request.user.is_staff or
                subscription.organization.memberships.filter(
                    user=request.user,
                    role='ADMIN',
                    is_active=True
                ).exists()):
            return Response(
                {"detail": "You don't have permission to cancel this subscription."},
                status=status.HTTP_403_FORBIDDEN
            )

        serializer = SubscriptionCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Add reason to notes if provided
        reason = serializer.validated_data.get('reason')
        if reason:
            subscription.notes = f"{subscription.notes}\n\nCancellation reason: {reason}".strip()

        # Cancel the subscription
        subscription.cancel()

        return Response(SubscriptionSerializer(subscription).data)

    @extend_schema(
        request=SubscriptionRenewSerializer,
        responses={200: SubscriptionSerializer}
    )
    @action(detail=True, methods=['post'], permission_classes=[IsSuperAdmin])
    def renew(self, request, pk=None):
        """Renew a subscription for another period"""
        subscription = self.get_object()
        serializer = SubscriptionRenewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        days = serializer.validated_data.get('days')
        subscription.renew(days)

        return Response(SubscriptionSerializer(subscription).data)

    @extend_schema(
        request=SubscriptionChangePlanSerializer,
        responses={200: SubscriptionSerializer}
    )
    @action(detail=True, methods=['post'], permission_classes=[IsSuperAdmin])
    def change_plan(self, request, pk=None):
        """Change the subscription to a different plan"""
        subscription = self.get_object()
        serializer = SubscriptionChangePlanSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        new_plan = serializer.validated_data['plan']
        subscription.change_plan(new_plan)

        return Response(SubscriptionSerializer(subscription).data)


@extend_schema(
    tags=["Subscriptions"],
    parameters=[
        OpenApiParameter(name="org_id", location=OpenApiParameter.PATH, required=True, type=str)
    ]
)
class OrganizationSubscriptionView(APIView):
    """
    API endpoint to get subscription details for a specific organization.

    Org admins and super admins can access this endpoint.
    """
    permission_classes = [IsAuthenticated]

    def get_permissions(self):
        """
        Check if user has permission to access this organization's subscription.
        """
        permissions = super().get_permissions()

        # If user is superadmin, they always have access
        if self.request.user.is_staff:
            return permissions

        # Check if organization ID is in URL and user is an admin of that org
        org_id = self.kwargs.get('org_id')
        if org_id:
            org_admin = self.request.user.memberships.filter(
                organization_id=org_id,
                role='ADMIN',
                is_active=True
            ).exists()

            if org_admin:
                return permissions

        # If not org admin or superadmin, deny access
        return [IsOrgAdmin()]

    @extend_schema(responses=SubscriptionSerializer)
    def get(self, request, org_id, *args, **kwargs):
        """Get subscription details for an organization"""
        try:
            organization = Organization.objects.get(id=org_id)
            subscription = Subscription.objects.select_related("organization", "plan").get(
                organization_id=org_id
            )
        except Organization.DoesNotExist:
            return Response({"detail": "Organization not found."}, status=status.HTTP_404_NOT_FOUND)
        except Subscription.DoesNotExist:
            return Response(
                {"detail": "No subscription for this organization."},
                status=status.HTTP_404_NOT_FOUND
            )

        return Response(SubscriptionSerializer(subscription).data)

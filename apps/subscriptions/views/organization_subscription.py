# apps/subscriptions/views/organization_subscription.py
"""Organization-scoped subscription detail view – converted from CBV to FBV."""

from drf_spectacular.utils import extend_schema, OpenApiParameter
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.organizations.models import Organization
from ..models import Subscription
from ..serializers import SubscriptionSerializer


@extend_schema(
    summary="Get organization subscription",
    description="Get subscription details for a specific organization. Org admins and super admins can access.",
    tags=["Plans & Subscriptions"],
    parameters=[
        OpenApiParameter(name="org_id", location=OpenApiParameter.PATH, required=True, type=str),
    ],
    responses={200: SubscriptionSerializer},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def organization_subscription_view(request, org_id):
    """Get subscription details for an organization."""
    user = request.user

    # Permission check – super admin always allowed; otherwise must be org admin
    if not user.is_staff:
        is_org_admin = user.memberships.filter(
            organization_id=org_id, role="ADMIN", is_active=True
        ).exists()
        if not is_org_admin:
            return Response(
                {"detail": "You do not have permission to access this resource."},
                status=status.HTTP_403_FORBIDDEN,
            )

    try:
        organization = Organization.objects.get(id=org_id)
        subscription = Subscription.objects.select_related("organization", "plan").get(
            organization_id=org_id,
        )
    except Organization.DoesNotExist:
        return Response({"detail": "Organization not found."}, status=status.HTTP_404_NOT_FOUND)
    except Subscription.DoesNotExist:
        return Response(
            {"detail": "No subscription for this organization."},
            status=status.HTTP_404_NOT_FOUND,
        )

    return Response(SubscriptionSerializer(subscription).data)

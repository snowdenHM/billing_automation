from rest_framework import viewsets, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.utils import extend_schema
from apps.common.permissions import IsSuperAdmin
from .models import Plan, Subscription
from .serializers import PlanSerializer, SubscriptionSerializer


@extend_schema(tags=["Plans"])
class PlanViewSet(viewsets.ModelViewSet):
    queryset = Plan.objects.all()
    serializer_class = PlanSerializer
    permission_classes = [IsAuthenticated]

    def get_permissions(self):
        # Only super admin can create/update/delete plans
        if self.action in ["create", "update", "partial_update", "destroy"]:
            return [IsSuperAdmin()]
        return super().get_permissions()


@extend_schema(tags=["Subscriptions"])
class SubscriptionAssignView(APIView):
    permission_classes = [IsAuthenticated, IsSuperAdmin]
    serializer_class = SubscriptionSerializer

    @extend_schema(request=SubscriptionSerializer, responses=SubscriptionSerializer)
    def post(self, request, *args, **kwargs):
        serializer = SubscriptionSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        sub = serializer.save()
        return Response(SubscriptionSerializer(sub).data, status=status.HTTP_201_CREATED)


@extend_schema(tags=["Subscriptions"])
class SubscriptionDetailView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = SubscriptionSerializer

    @extend_schema(responses=SubscriptionSerializer)
    def get(self, request, org_id, *args, **kwargs):
        try:
            sub = Subscription.objects.select_related("organization", "plan").get(organization_id=org_id)
        except Subscription.DoesNotExist:
            return Response({"detail": "No subscription for this organization."}, status=404)
        return Response(SubscriptionSerializer(sub).data)

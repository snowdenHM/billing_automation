from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    PlanViewSet,
    SubscriptionViewSet,
    OrganizationSubscriptionView,
)

# Setup DRF routers
router = DefaultRouter()
router.register(r"plans", PlanViewSet, basename="plan")
router.register(r"subscriptions", SubscriptionViewSet, basename="subscription")

app_name = "subscriptions"

urlpatterns = [
    # Organization subscription detail view (uses UUID)
    path("organizations/<uuid:org_id>/subscription/",
         OrganizationSubscriptionView.as_view(),
         name="organization-subscription"),

    # Include router URLs
    path("", include(router.urls)),
]

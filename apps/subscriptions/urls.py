from django.urls import path
from rest_framework.routers import DefaultRouter
from .views import PlanViewSet, SubscriptionAssignView, SubscriptionDetailView

router = DefaultRouter()
router.register(r"subscriptions/plans", PlanViewSet, basename="plan")

urlpatterns = [
    path("subscriptions/assign/", SubscriptionAssignView.as_view(), name="subscription-assign"),
    path("subscriptions/<int:org_id>/", SubscriptionDetailView.as_view(), name="subscription-detail"),
]
urlpatterns += router.urls
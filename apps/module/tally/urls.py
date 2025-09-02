from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import LedgerViewSet, TallyConfigViewSet
from .vendor_views import TallyVendorBillViewSet
from .expense_views import TallyExpenseBillViewSet

# Create router for the viewsets
router = DefaultRouter()
router.register(r'configs', TallyConfigViewSet, basename='tally-config')
router.register(r'vendor-bills', TallyVendorBillViewSet, basename='vendor-bill')
router.register(r'expense-bills', TallyExpenseBillViewSet, basename='expense-bill')

app_name = 'tally'

urlpatterns = [
    # Organization-scoped endpoints (UUID only)
    path('org/<uuid:org_id>/', include([
        path('', include(router.urls)),
        # Custom ledger endpoints (only GET and POST)
        path('ledgers/', LedgerViewSet.as_view({'get': 'list', 'post': 'create'}), name='ledger-list'),
    ])),
]

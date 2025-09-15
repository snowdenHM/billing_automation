from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .expense_views import TallyExpenseBillViewSet
from .vendor_views_functional import (
    vendor_bills_list,
    vendor_bills_upload,
    vendor_bill_detail,
    vendor_bill_delete,
    vendor_bill_analyze,
    vendor_bill_verify,
    vendor_bill_sync,
    vendor_bills_sync_list,
    vendor_bill_sync_external
)
from .views import LedgerViewSet, TallyConfigViewSet

# Create router for the viewsets
router = DefaultRouter()
router.register(r'configs', TallyConfigViewSet, basename='tally-config')
router.register(r'expense-bills', TallyExpenseBillViewSet, basename='expense-bill')

app_name = 'tally'

urlpatterns = [
    # Organization-scoped endpoints (UUID only)
    path('org/<uuid:org_id>/', include([
        path('', include(router.urls)),
        # Custom ledger endpoints (only GET and POST)
        path('ledgers/', LedgerViewSet.as_view({'get': 'list', 'post': 'create'}), name='ledger-list'),

        # Function-based vendor bill endpoints
        path('vendor-bills/', vendor_bills_list, name='vendor-bills-list'),
        path('vendor-bills/upload/', vendor_bills_upload, name='vendor-bills-upload'),
        path('vendor-bills/<uuid:bill_id>/delete/', vendor_bill_delete, name='vendor-bill-delete'),
        path('vendor-bills/analyze/', vendor_bill_analyze, name='vendor-bill-analyze'),
        path('vendor-bills/<uuid:bill_id>/details/', vendor_bill_detail, name='vendor-bill-detail'),
        path('vendor-bills/verify/', vendor_bill_verify, name='vendor-bill-verify'),
        path('vendor-bills/sync/', vendor_bill_sync, name='vendor-bill-sync'),
        path('vendor-bills/sync_bills/', vendor_bills_sync_list, name='vendor-bills-sync-list'),
        path('vendor-bills/sync_external/', vendor_bill_sync_external, name='vendor-bill-sync-external'),
    ])),
]

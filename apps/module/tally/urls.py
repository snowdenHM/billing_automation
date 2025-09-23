from django.urls import path, include
from rest_framework.routers import DefaultRouter

# Import functional expense views
from .expense_views_functional import (
    expense_bills_list,
    expense_bills_upload,
    expense_bill_detail,
    expense_bill_delete,
    expense_bill_analyze,
    expense_bill_verify,
    expense_bill_sync,
    expense_bills_sync_list,
    expense_bill_sync_external
)
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
from .views import LedgerViewSet, TallyConfigViewSet, MasterAPIView

# Create router for the remaining viewsets
router = DefaultRouter()
router.register(r'configs', TallyConfigViewSet, basename='tally-config')

app_name = 'tally'

urlpatterns = [
    # Organization-scoped endpoints (UUID only)
    path('org/<uuid:org_id>/', include([
        path('', include(router.urls)),
        # Custom ledger endpoints (only GET and POST)
        path('ledgers/', LedgerViewSet.as_view({'get': 'list', 'post': 'create'}), name='ledger-list'),

        # Master API for capturing incoming Tally data
        path('masters/', MasterAPIView.as_view(), name='master-api'),

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

        # Function-based expense bill endpoints
        path('expense-bills/', expense_bills_list, name='expense-bills-list'),
        path('expense-bills/upload/', expense_bills_upload, name='expense-bills-upload'),
        path('expense-bills/<uuid:bill_id>/delete/', expense_bill_delete, name='expense-bill-delete'),
        path('expense-bills/analyze/', expense_bill_analyze, name='expense-bill-analyze'),
        path('expense-bills/<uuid:bill_id>/details/', expense_bill_detail, name='expense-bill-detail'),
        path('expense-bills/verify/', expense_bill_verify, name='expense-bill-verify'),
        path('expense-bills/sync/', expense_bill_sync, name='expense-bill-sync'),
        path('expense-bills/sync_bills/', expense_bills_sync_list, name='expense-bills-sync-list'),
        path('expense-bills/sync_external/', expense_bill_sync_external, name='expense-bill-sync-external'),
    ])),
]

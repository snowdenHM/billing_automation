from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    # Config
    get_tally_config,
    create_or_update_tally_config,
    TallyConfigViewSet,
    # Ledger / Parent Ledger / Master
    LedgerViewSet,
    ParentLedgerViewSet,
    MasterAPIView,
    # Bill status
    update_bill_tally_sync_status,
    # Vendor bills
    vendor_bills_list,
    vendor_bills_upload,
    vendor_bill_detail,
    vendor_bill_delete,
    vendor_bill_analyze,
    vendor_bill_verify,
    vendor_bill_sync,
    vendor_bills_sync_list,
    vendor_bill_sync_external,
    # Expense bills
    expense_bills_list,
    expense_bills_upload,
    expense_bill_detail,
    expense_bill_delete,
    expense_bill_analyze,
    expense_bill_verify,
    expense_bill_sync,
    expense_bills_sync_list,
    expense_bill_sync_external,
    # Bill move
    move_tally_bills_between_modules_view,
    # Organization data
    organization_tally_data,
)

# Create router for the remaining viewsets
router = DefaultRouter()
router.register(r'configs', TallyConfigViewSet, basename='tally-config')
router.register(r'parent-ledgers', ParentLedgerViewSet, basename='parent-ledger')

app_name = 'tally'

urlpatterns = [
    # Organization-scoped endpoints (UUID only)  
    path('org/<uuid:org_id>/', include([
        # New function-based Tally Config endpoints
        path('config/', get_tally_config, name='get-tally-config'),
        path('config/save/', create_or_update_tally_config, name='create-update-tally-config'),
        
        # Other router URLs
        path('', include(router.urls)),

        # Organization comprehensive data endpoint
        path('help/', organization_tally_data, name='organization-tally-data'),

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

        # Tally Bill Moving Between Modules
        path('bills/move/', move_tally_bills_between_modules_view, name='tally-bills-move'),

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

        # Bill tally sync status update endpoint
        path('bills/tally_status/', update_bill_tally_sync_status, name='update-bill-tally-sync-status'),
    ])),
]

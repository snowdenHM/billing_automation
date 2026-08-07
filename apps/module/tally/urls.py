from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    # Config
    get_tally_config,
    create_or_update_tally_config,
    TallyConfigViewSet,
    list_gst_rate_ledger_mappings,
    upsert_gst_rate_ledger_mappings,
    delete_gst_rate_ledger_mapping,
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
    # Payment vouchers
    payment_bills_list,
    payment_bills_upload,
    payment_bill_detail,
    payment_bill_delete,
    payment_bill_analyze,
    payment_bill_verify,
    payment_bill_sync,
    payment_bills_sync_list,
    payment_bill_sync_external,
)
from .views.bill_reports import (
    tally_vendor_bills_report,
    tally_expense_bills_report,
    tally_payment_bills_report,
)
from .views.quick_create import (
    quick_create_vendor,
    quick_create_ledger,
    quick_create_item,
)
from .views.master_sync import (
    masters_pending_sync,
    masters_mark_synced,
)
from .views import (
    # Bill move
    move_tally_bills_between_modules_view,
    # Organization data
    organization_tally_data,
    # Setup guide
    tally_setup_guide,
    # Bill image scanner
    scan_process,
    # Trash
    trash_list,
    trash_restore,
    trash_delete_forever,
    trash_empty,
)

# Create router for the remaining viewsets
router = DefaultRouter()
router.register(r'configs', TallyConfigViewSet, basename='tally-config')
router.register(r'parent-ledgers', ParentLedgerViewSet, basename='parent-ledger')

app_name = 'tally'

urlpatterns = [
    # Global (non-org-scoped) Tally setup guide — admin-managed walkthrough
    path('setup-guide/', tally_setup_guide, name='tally-setup-guide'),

    # Organization-scoped endpoints (UUID only)
    path('org/<uuid:org_id>/', include([
        # New function-based Tally Config endpoints
        path('config/', get_tally_config, name='get-tally-config'),
        path('config/save/', create_or_update_tally_config, name='create-update-tally-config'),

        # CamScanner-style bill image enhancement (used by upload modal)
        path('scan/process/', scan_process, name='scan-process'),

        # GST Rate → Ledger mapping endpoints (used for per-line tax assignment)
        path('config/gst-rate-mappings/', list_gst_rate_ledger_mappings, name='gst-rate-mappings-list'),
        path('config/gst-rate-mappings/save/', upsert_gst_rate_ledger_mappings, name='gst-rate-mappings-upsert'),
        path('config/gst-rate-mappings/<uuid:mapping_id>/delete/', delete_gst_rate_ledger_mapping, name='gst-rate-mappings-delete'),
        
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

        # Quick-create masters from bill detail / standalone pages
        path('quick-create/vendor/', quick_create_vendor, name='quick-create-vendor'),
        path('quick-create/ledger/', quick_create_ledger, name='quick-create-ledger'),
        path('quick-create/item/',   quick_create_item,   name='quick-create-item'),

        # Master-sync contract — Tally TCP polls pending_sync then callbacks mark_synced
        path('masters/pending_sync/', masters_pending_sync, name='masters-pending-sync'),
        path('masters/mark_synced/',  masters_mark_synced,  name='masters-mark-synced'),

        # XLSX report downloads (one per bill type, ?status= filter)
        path('vendor-bills/report/', tally_vendor_bills_report, name='vendor-bills-report'),
        path('expense-bills/report/', tally_expense_bills_report, name='expense-bills-report'),
        path('payment-vouchers/report/', tally_payment_bills_report, name='payment-vouchers-report'),

        # Function-based payment voucher endpoints
        path('payment-vouchers/', payment_bills_list, name='payment-vouchers-list'),
        path('payment-vouchers/upload/', payment_bills_upload, name='payment-vouchers-upload'),
        path('payment-vouchers/<uuid:bill_id>/delete/', payment_bill_delete, name='payment-voucher-delete'),
        path('payment-vouchers/analyze/', payment_bill_analyze, name='payment-voucher-analyze'),
        path('payment-vouchers/<uuid:bill_id>/details/', payment_bill_detail, name='payment-voucher-detail'),
        path('payment-vouchers/verify/', payment_bill_verify, name='payment-voucher-verify'),
        path('payment-vouchers/sync/', payment_bill_sync, name='payment-voucher-sync'),
        path('payment-vouchers/sync_bills/', payment_bills_sync_list, name='payment-vouchers-sync-list'),
        path('payment-vouchers/sync_external/', payment_bill_sync_external, name='payment-voucher-sync-external'),

        # Bill tally sync status update endpoint
        path('bills/tally_status/', update_bill_tally_sync_status, name='update-bill-tally-sync-status'),

        # Trash — recoverable delete across every Tally bill type.
        # `empty/` is declared before the <slug> route so it can never be
        # swallowed as a document type.
        path('trash/', trash_list, name='trash-list'),
        path('trash/empty/', trash_empty, name='trash-empty'),
        path('trash/<slug:kind_slug>/<uuid:bill_id>/restore/', trash_restore, name='trash-restore'),
        path('trash/<slug:kind_slug>/<uuid:bill_id>/', trash_delete_forever, name='trash-delete-forever'),
    ])),
]

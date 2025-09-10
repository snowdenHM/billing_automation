from django.urls import path, include

# Import Zoho settings and sync views from main views.py
from .views import (
    # Zoho Settings/Credentials
    zoho_credentials_view,
    generate_token_view,

    # Zoho Sync Endpoints
    vendors_list_view,
    vendors_sync_view,
    chart_of_accounts_list_view,
    chart_of_accounts_sync_view,
    taxes_list_view,
    taxes_sync_view,
    tds_tcs_list_view,
)

# Import vendor bill views from vendor_views.py
from .vendor_views import (
    # Vendor Bills Workflow
    vendor_bills_list_view as vendor_bills_list_main,
    vendor_bill_upload_view,
    vendor_bill_detail_view as vendor_bill_detail_main,
    vendor_bill_image_view,
    vendor_bill_analyze_view as vendor_bill_analyze_main,
    vendor_bill_verify_view as vendor_bill_verify_main,
    vendor_bill_sync_view as vendor_bill_sync_main,
    vendor_bill_delete_view,

    # Filtered Vendor Views
    vendor_bills_draft_view,
    vendor_bills_analyzed_view,
    vendor_bills_synced_view,
    vendor_product_update_view as vendor_product_update_main,
)

# Import expense bill views from expense_views.py
from .expense_views import (
    # Expense Bills Workflow
    expense_bills_list_view,
    expense_bill_upload_view,
    expense_bill_detail_view,
    expense_bill_image_view,
    expense_bill_analyze_view,
    expense_bill_verify_view,
    expense_bill_sync_view,
    expense_bill_delete_view,

    # Filtered Expense Views
    expense_bills_draft_view,
    expense_bills_analyzed_view,
    expense_bills_synced_view,
)

app_name = "zoho"

urlpatterns = [
    # All endpoints are now organization-scoped
    path('org/<uuid:org_id>/', include([

        # ============================================================================
        # Zoho Settings/Credentials Management
        # ============================================================================
        path('credentials/', zoho_credentials_view, name='zoho_credentials'),
        path('generate-token/', generate_token_view, name='generate_token'),

        # ============================================================================
        # Zoho Sync Endpoints
        # ============================================================================
        path('vendors/', vendors_list_view, name='vendors_list'),
        path('vendors/sync/', vendors_sync_view, name='vendors_sync'),
        path('chart-of-accounts/', chart_of_accounts_list_view, name='chart_of_accounts_list'),
        path('chart-of-accounts/sync/', chart_of_accounts_sync_view, name='chart_of_accounts_sync'),
        path('taxes/', taxes_list_view, name='taxes_list'),
        path('taxes/sync/', taxes_sync_view, name='taxes_sync'),
        path('tds-tcs/', tds_tcs_list_view, name='tds_tcs_list'),

        # ============================================================================
        # Vendor Bills API Endpoints
        # ============================================================================
        path('vendor-bills/', vendor_bills_list_main, name='vendor_bills_list'),
        path('vendor-bills/upload/', vendor_bill_upload_view, name='vendor_bills_upload'),
        path('vendor-bills/draft/', vendor_bills_draft_view, name='vendor_bills_draft'),
        path('vendor-bills/analyzed/', vendor_bills_analyzed_view, name='vendor_bills_analyzed'),
        path('vendor-bills/synced/', vendor_bills_synced_view, name='vendor_bills_synced'),
        path('vendor-bills/<int:bill_id>/', vendor_bill_detail_main, name='vendor_bill_detail'),
        path('vendor-bills/<int:bill_id>/image/', vendor_bill_image_view, name='vendor_bill_image'),
        path('vendor-bills/<int:bill_id>/analyze/', vendor_bill_analyze_main, name='vendor_bill_analyze'),
        path('vendor-bills/<int:bill_id>/verify/', vendor_bill_verify_main, name='vendor_bill_verify'),
        path('vendor-bills/<int:bill_id>/sync/', vendor_bill_sync_main, name='vendor_bill_sync'),
        path('vendor-bills/<int:bill_id>/delete/', vendor_bill_delete_view, name='vendor_bill_delete'),
        path('vendor-products/<int:product_id>/update/', vendor_product_update_main, name='vendor_product_update'),

        # ============================================================================
        # Expense Bills API Endpoints
        # ============================================================================
        path('expense-bills/', expense_bills_list_view, name='expense_bills_list'),
        path('expense-bills/upload/', expense_bill_upload_view, name='expense_bills_upload'),
        path('expense-bills/draft/', expense_bills_draft_view, name='expense_bills_draft'),
        path('expense-bills/analyzed/', expense_bills_analyzed_view, name='expense_bills_analyzed'),
        path('expense-bills/synced/', expense_bills_synced_view, name='expense_bills_synced'),
        path('expense-bills/<int:bill_id>/', expense_bill_detail_view, name='expense_bill_detail'),
        path('expense-bills/<int:bill_id>/image/', expense_bill_image_view, name='expense_bill_image'),
        path('expense-bills/<int:bill_id>/analyze/', expense_bill_analyze_view, name='expense_bill_analyze'),
        path('expense-bills/<int:bill_id>/verify/', expense_bill_verify_view, name='expense_bill_verify'),
        path('expense-bills/<int:bill_id>/sync/', expense_bill_sync_view, name='expense_bill_sync'),
        path('expense-bills/<int:bill_id>/delete/', expense_bill_delete_view, name='expense_bill_delete'),
    ])),
]

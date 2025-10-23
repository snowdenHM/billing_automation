from django.urls import path, include

# Import expense bill views from expense_views.py
from .journal_views import (
    # Journal Bills Workflow
    journal_bills_list_view,
    journal_bill_upload_view,
    journal_bill_detail_view,
    journal_bill_analyze_view,
    journal_bill_verify_view,
    journal_bill_sync_view,
    journal_bill_delete_view,
)
# Import vendor bill views from vendor_views.py
from .vendor_views import (
    # Vendor Bills Workflow
    vendor_bills_list_view as vendor_bills_list_main,
    vendor_bill_upload_view,
    vendor_bill_detail_view as vendor_bill_detail_main,
    vendor_bill_analyze_view as vendor_bill_analyze_main,
    vendor_bill_verify_view as vendor_bill_verify_main,
    vendor_bill_sync_view as vendor_bill_sync_main,
    vendor_bill_delete_view,
)
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
    tds_tcs_sync_view,
)

# Import expense bill views from expense_views.py
from .expense_views import (
    expense_bills_list_view,
    expense_bill_upload_view,
    expense_bill_detail_view,
    expense_bill_analyze_view,
    expense_bill_verify_view,
    expense_bill_sync_view,
    expense_bill_delete_view,
)

app_name = "zoho"

urlpatterns = [
    # All endpoints are now organization-scoped
    path('org/<uuid:org_id>/', include([

        # ============================================================================
        # Zoho Settings/Credentials Management
        # ============================================================================
        path('settings/credentials/', zoho_credentials_view, name='zoho_credentials'),
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
        path('tds-tcs/sync/', tds_tcs_sync_view, name='tds_tcs_sync'),

        # ============================================================================
        # Vendor Bills API Endpoints
        # ============================================================================
        path('vendor-bills/', vendor_bills_list_main, name='vendor_bills_list'),
        path('vendor-bills/upload/', vendor_bill_upload_view, name='vendor_bills_upload'),
        path('vendor-bills/<str:bill_id>/details/', vendor_bill_detail_main, name='vendor_bill_detail'),
        path('vendor-bills/<str:bill_id>/analyze/', vendor_bill_analyze_main, name='vendor_bill_analyze'),
        path('vendor-bills/<str:bill_id>/verify/', vendor_bill_verify_main, name='vendor_bill_verify'),
        path('vendor-bills/<str:bill_id>/sync/', vendor_bill_sync_main, name='vendor_bill_sync'),
        path('vendor-bills/<str:bill_id>/delete/', vendor_bill_delete_view, name='vendor_bill_delete'),

        # ============================================================================
        # Journal Bills API Endpoints
        # ============================================================================
        path('journal-bills/', journal_bills_list_view, name='journal_bills_list'),
        path('journal-bills/upload/', journal_bill_upload_view, name='journal_bills_upload'),
        path('journal-bills/<str:bill_id>/details/', journal_bill_detail_view, name='journal_bill_detail'),
        path('journal-bills/<str:bill_id>/analyze/', journal_bill_analyze_view, name='journal_bill_analyze'),
        path('journal-bills/<str:bill_id>/verify/', journal_bill_verify_view, name='journal_bill_verify'),
        path('journal-bills/<str:bill_id>/sync/', journal_bill_sync_view, name='journal_bill_sync'),
        path('journal-bills/<str:bill_id>/delete/', journal_bill_delete_view, name='journal_bill_delete'),

        # ============================================================================
        # Expense Bills API Endpoints
        # ============================================================================
        path('expense-bills/', expense_bills_list_view, name='expense_bills_list'),
        path('expense-bills/upload/', expense_bill_upload_view, name='expense_bills_upload'),
        path('expense-bills/<str:bill_id>/details/', expense_bill_detail_view, name='expense_bill_detail'),
        path('expense-bills/<str:bill_id>/analyze/', expense_bill_analyze_view, name='expense_bill_analyze'),
        path('expense-bills/<str:bill_id>/verify/', expense_bill_verify_view, name='expense_bill_verify'),
        path('expense-bills/<str:bill_id>/sync/', expense_bill_sync_view, name='expense_bill_sync'),
        path('expense-bills/<str:bill_id>/delete/', expense_bill_delete_view, name='expense_bill_delete'),
    ])),
]

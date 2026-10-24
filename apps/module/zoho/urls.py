from django.urls import path, include

# Bill image scanner is shared between Tally and Zoho — both upload flows use
# the same CamScanner-style preprocessing helper before OCR runs.
from apps.module.tally.views.scan import scan_process as bill_scan_process

from .views import (
    # Journal bills
    journal_bills_list_view,
    journal_bill_upload_view,
    journal_bill_detail_view,
    journal_bill_analyze_view,
    journal_bill_verify_view,
    journal_bill_sync_view,
    journal_bill_delete_view,
    # Vendor bills
    vendor_bills_list_view,
    vendor_bill_upload_view,
    vendor_bill_detail_view,
    vendor_bill_analyze_view,
    vendor_bill_verify_view,
    vendor_bill_sync_view,
    vendor_bill_delete_view,
    move_bill_between_modules_view,
    # Credentials / OAuth
    zoho_credentials_view,
    generate_token_view,
    initiate_oauth_view,
    oauth_callback_view,
    zoho_status_view,
    # Sync
    vendors_list_view,
    vendors_sync_view,
    chart_of_accounts_list_view,
    chart_of_accounts_sync_view,
    taxes_list_view,
    taxes_sync_view,
    tds_tcs_list_view,
    tds_tcs_sync_view,
    customers_list_view,
    customers_sync_view,
    # Expense bills
    expense_bills_list_view,
    expense_bill_upload_view,
    expense_bill_detail_view,
    expense_bill_analyze_view,
    expense_bill_verify_view,
    expense_bill_sync_view,
    expense_bill_delete_view,
    # Sales invoices
    sales_bills_list_view,
    sales_bill_upload_view,
    sales_bill_detail_view,
    sales_bill_analyze_view,
    sales_bill_verify_view,
    sales_bill_sync_view,
    sales_bill_delete_view,
    # Receipt vouchers
    receipt_bills_list_view,
    receipt_bill_upload_view,
    receipt_bill_detail_view,
    receipt_bill_analyze_view,
    receipt_bill_verify_view,
    receipt_bill_sync_view,
    receipt_bill_delete_view,
    # Payment vouchers
    payment_bills_list_view,
    payment_bill_upload_view,
    payment_bill_detail_view,
    payment_bill_analyze_view,
    payment_bill_verify_view,
    payment_bill_sync_view,
    payment_bill_delete_view,
)
from .views.bill_reports import (
    zoho_vendor_bills_report,
    zoho_journal_bills_report,
    zoho_expense_bills_report,
    zoho_sales_bills_report,
    zoho_receipt_bills_report,
    zoho_payment_bills_report,
)

app_name = "zoho"

urlpatterns = [
    # All endpoints are now organization-scoped
    path('org/<uuid:org_id>/', include([

        # ============================================================================
        # Bill image scanner (shared with Tally module)
        # ============================================================================
        path('scan/process/', bill_scan_process, name='zoho-scan-process'),

        # ============================================================================
        # Zoho Settings/Credentials Management
        # ============================================================================
        path('status/', zoho_status_view, name='zoho_status'),
        path('settings/credentials/', zoho_credentials_view, name='zoho_credentials'),
        path('oauth/initiate/', initiate_oauth_view, name='initiate_oauth'),
        path('oauth/callback/', oauth_callback_view, name='oauth_callback'),
        path('generate-token/', generate_token_view, name='generate_token'),

        # ============================================================================
        # Zoho Sync Endpoints
        # ============================================================================
        path('vendors/', vendors_list_view, name='vendors_list'),
        path('vendors/sync/', vendors_sync_view, name='vendors_sync'),
        path('customers/', customers_list_view, name='customers_list'),
        path('customers/sync/', customers_sync_view, name='customers_sync'),
        path('chart-of-accounts/', chart_of_accounts_list_view, name='chart_of_accounts_list'),
        path('chart-of-accounts/sync/', chart_of_accounts_sync_view, name='chart_of_accounts_sync'),
        path('taxes/', taxes_list_view, name='taxes_list'),
        path('taxes/sync/', taxes_sync_view, name='taxes_sync'),
        path('tds-tcs/', tds_tcs_list_view, name='tds_tcs_list'),
        path('tds-tcs/sync/', tds_tcs_sync_view, name='tds_tcs_sync'),

        # ============================================================================
        # Bill Moving Between Modules API Endpoints
        # ============================================================================
        path('bills/move/', move_bill_between_modules_view, name='move_bill_between_modules'),

        # ============================================================================
        # Vendor Bills API Endpoints
        # ============================================================================
        path('vendor-bills/', vendor_bills_list_view, name='vendor_bills_list'),
        path('vendor-bills/upload/', vendor_bill_upload_view, name='vendor_bills_upload'),
        path('vendor-bills/<str:bill_id>/details/', vendor_bill_detail_view, name='vendor_bill_detail'),
        path('vendor-bills/<str:bill_id>/analyze/', vendor_bill_analyze_view, name='vendor_bill_analyze'),
        path('vendor-bills/<str:bill_id>/verify/', vendor_bill_verify_view, name='vendor_bill_verify'),
        path('vendor-bills/<str:bill_id>/sync/', vendor_bill_sync_view, name='vendor_bill_sync'),
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

        # ============================================================================
        # Sales Invoices API Endpoints
        # ============================================================================
        path('sales-bills/', sales_bills_list_view, name='sales_bills_list'),
        path('sales-bills/upload/', sales_bill_upload_view, name='sales_bills_upload'),
        path('sales-bills/<str:bill_id>/details/', sales_bill_detail_view, name='sales_bill_detail'),
        path('sales-bills/<str:bill_id>/analyze/', sales_bill_analyze_view, name='sales_bill_analyze'),
        path('sales-bills/<str:bill_id>/verify/', sales_bill_verify_view, name='sales_bill_verify'),
        path('sales-bills/<str:bill_id>/sync/', sales_bill_sync_view, name='sales_bill_sync'),
        path('sales-bills/<str:bill_id>/delete/', sales_bill_delete_view, name='sales_bill_delete'),

        # ============================================================================
        # Receipt Vouchers API Endpoints
        # ============================================================================
        path('receipt-bills/', receipt_bills_list_view, name='receipt_bills_list'),
        path('receipt-bills/upload/', receipt_bill_upload_view, name='receipt_bills_upload'),
        path('receipt-bills/<str:bill_id>/details/', receipt_bill_detail_view, name='receipt_bill_detail'),
        path('receipt-bills/<str:bill_id>/analyze/', receipt_bill_analyze_view, name='receipt_bill_analyze'),
        path('receipt-bills/<str:bill_id>/verify/', receipt_bill_verify_view, name='receipt_bill_verify'),
        path('receipt-bills/<str:bill_id>/sync/', receipt_bill_sync_view, name='receipt_bill_sync'),
        path('receipt-bills/<str:bill_id>/delete/', receipt_bill_delete_view, name='receipt_bill_delete'),

        # ============================================================================
        # Payment Vouchers API Endpoints
        # ============================================================================
        path('payment-bills/', payment_bills_list_view, name='payment_bills_list'),
        path('payment-bills/upload/', payment_bill_upload_view, name='payment_bills_upload'),
        path('payment-bills/<str:bill_id>/details/', payment_bill_detail_view, name='payment_bill_detail'),
        path('payment-bills/<str:bill_id>/analyze/', payment_bill_analyze_view, name='payment_bill_analyze'),
        path('payment-bills/<str:bill_id>/verify/', payment_bill_verify_view, name='payment_bill_verify'),
        path('payment-bills/<str:bill_id>/sync/', payment_bill_sync_view, name='payment_bill_sync'),
        path('payment-bills/<str:bill_id>/delete/', payment_bill_delete_view, name='payment_bill_delete'),

        # ============================================================================
        # XLSX Report Downloads (?status=Analysed,Verified,Synced filter)
        # ============================================================================
        path('vendor-bills/report/', zoho_vendor_bills_report, name='vendor_bills_report'),
        path('journal-bills/report/', zoho_journal_bills_report, name='journal_bills_report'),
        path('expense-bills/report/', zoho_expense_bills_report, name='expense_bills_report'),
        path('sales-bills/report/', zoho_sales_bills_report, name='sales_bills_report'),
        path('receipt-bills/report/', zoho_receipt_bills_report, name='receipt_bills_report'),
        path('payment-bills/report/', zoho_payment_bills_report, name='payment_bills_report'),
    ])),
]

# apps/module/zoho/views/__init__.py
"""
Zoho views package.
Re-exports all view functions so existing imports continue to work.
"""

# Helpers
from .helpers import (
    get_organization_from_request,
    get_zoho_credentials,
    make_zoho_api_request,
)

# Credentials & OAuth
from .credentials import (
    zoho_credentials_view,
    zoho_connection_test_view,
    generate_token_view,
    initiate_oauth_view,
    oauth_callback_view,
    zoho_status_view,
)

# Data sync
from .sync import (
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
)

# Vendor bills
from .vendor_bills import (
    vendor_bills_list_view,
    vendor_bill_upload_view,
    vendor_bill_detail_view,
    vendor_bill_analyze_view,
    vendor_bill_verify_view,
    vendor_bill_sync_view,
    vendor_bill_delete_view,
    move_bill_between_modules_view,
)

# Expense bills
from .expense_bills import (
    expense_bills_list_view,
    expense_bill_upload_view,
    expense_bill_detail_view,
    expense_bill_analyze_view,
    expense_bill_verify_view,
    expense_bill_sync_view,
    expense_bill_delete_view,
)

# Journal bills
from .journal_bills import (
    journal_bills_list_view,
    journal_bill_upload_view,
    journal_bill_detail_view,
    journal_bill_analyze_view,
    journal_bill_verify_view,
    journal_bill_sync_view,
    journal_bill_delete_view,
)

# Sales invoices
from .sales_bills import (
    sales_bills_list_view,
    sales_bill_upload_view,
    sales_bill_detail_view,
    sales_bill_analyze_view,
    sales_bill_verify_view,
    sales_bill_sync_view,
    sales_bill_delete_view,
)

# Receipt vouchers
from .receipt_bills import (
    receipt_bills_list_view,
    receipt_bill_upload_view,
    receipt_bill_detail_view,
    receipt_bill_analyze_view,
    receipt_bill_verify_view,
    receipt_bill_sync_view,
    receipt_bill_delete_view,
)

# Payment vouchers
from .payment_bills import (
    payment_bills_list_view,
    payment_bill_upload_view,
    payment_bill_detail_view,
    payment_bill_analyze_view,
    payment_bill_verify_view,
    payment_bill_sync_view,
    payment_bill_delete_view,
)

__all__ = [
    # Helpers
    "get_organization_from_request",
    "get_zoho_credentials",
    "make_zoho_api_request",
    # Credentials
    "zoho_credentials_view",
    "zoho_connection_test_view",
    "generate_token_view",
    "initiate_oauth_view",
    "oauth_callback_view",
    "zoho_status_view",
    # Sync
    "vendors_list_view",
    "vendors_sync_view",
    "chart_of_accounts_list_view",
    "chart_of_accounts_sync_view",
    "taxes_list_view",
    "taxes_sync_view",
    "tds_tcs_list_view",
    "tds_tcs_sync_view",
    "customers_list_view",
    "customers_sync_view",
    # Vendor bills
    "vendor_bills_list_view",
    "vendor_bill_upload_view",
    "vendor_bill_detail_view",
    "vendor_bill_analyze_view",
    "vendor_bill_verify_view",
    "vendor_bill_sync_view",
    "vendor_bill_delete_view",
    "move_bill_between_modules_view",
    # Expense bills
    "expense_bills_list_view",
    "expense_bill_upload_view",
    "expense_bill_detail_view",
    "expense_bill_analyze_view",
    "expense_bill_verify_view",
    "expense_bill_sync_view",
    "expense_bill_delete_view",
    # Journal bills
    "journal_bills_list_view",
    "journal_bill_upload_view",
    "journal_bill_detail_view",
    "journal_bill_analyze_view",
    "journal_bill_verify_view",
    "journal_bill_sync_view",
    "journal_bill_delete_view",
    # Sales invoices
    "sales_bills_list_view",
    "sales_bill_upload_view",
    "sales_bill_detail_view",
    "sales_bill_analyze_view",
    "sales_bill_verify_view",
    "sales_bill_sync_view",
    "sales_bill_delete_view",
    # Receipt vouchers
    "receipt_bills_list_view",
    "receipt_bill_upload_view",
    "receipt_bill_detail_view",
    "receipt_bill_analyze_view",
    "receipt_bill_verify_view",
    "receipt_bill_sync_view",
    "receipt_bill_delete_view",
    # Payment vouchers
    "payment_bills_list_view",
    "payment_bill_upload_view",
    "payment_bill_detail_view",
    "payment_bill_analyze_view",
    "payment_bill_verify_view",
    "payment_bill_sync_view",
    "payment_bill_delete_view",
]

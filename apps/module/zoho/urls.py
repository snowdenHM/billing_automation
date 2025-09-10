from django.urls import path, include

from .views import (
    # Zoho Sync Endpoints
    vendors_list_view,
    chart_of_accounts_list_view,
    taxes_list_view,
    tds_tcs_list_view,

    # Vendor Bills Workflow
    vendor_bills_list_view,
    vendor_bills_upload_view,
    vendor_bill_detail_view,
    vendor_bill_verify_view,

    # Expense Bills Workflow
    expense_bills_list_view,
    expense_bills_upload_view,
    expense_bill_detail_view,
    expense_bill_verify_view,

    # Zoho Settings
    zoho_credentials_view,
)

from .class_views import (
    # Zoho Settings
    GenerateTokenView,

    # Zoho Sync Endpoints
    VendorsSyncView,
    ChartOfAccountsSyncView,
    TaxesSyncView,
    TdsTcsSyncView,

    # Vendor Bills Workflow
    VendorBillAnalyzeView,
    VendorBillSyncView,

    # Expense Bills Workflow
    ExpenseBillAnalyzeView,
    ExpenseBillSyncView,
)

app_name = "zoho"

urlpatterns = [
    # Organization-scoped endpoints (UUID only)
    path('org/<uuid:org_id>/', include([
        # Zoho Settings/Credentials
        path("settings/credentials/", zoho_credentials_view, name="zoho-credentials"),
        path("settings/generate-token/", GenerateTokenView.as_view(), name="generate-token"),

        # Zoho Sync Endpoints (GET and SYNC only)
        path("vendors/", vendors_list_view, name="vendors-list"),
        path("vendors/sync/", VendorsSyncView.as_view(), name="vendors-sync"),
        path("chart-of-accounts/", chart_of_accounts_list_view, name="chart-of-accounts-list"),
        path("chart-of-accounts/sync/", ChartOfAccountsSyncView.as_view(), name="chart-of-accounts-sync"),
        path("vendors/gst/", fetch_vendor_gst_view, name="fetch-vendor-gst"),
        path("taxes/", taxes_list_view, name="taxes-list"),
        path("taxes/sync/", TaxesSyncView.as_view(), name="taxes-sync"),
        path("tds-tcs/", tds_tcs_list_view, name="tds-tcs-list"),
        path("tds-tcs/sync/", TdsTcsSyncView.as_view(), name="tds-tcs-sync"),

        # Vendor Bills Workflow (Draft → Analyzed → Verified → Synced)
        path("vendor-bills/", vendor_bills_list_view, name="vendor-bills-list"),
        path("vendor-bills/upload/", vendor_bills_upload_view, name="vendor-bills-upload"),
        path("vendor-bills/<uuid:bill_id>/", vendor_bill_detail_view, name="vendor-bill-detail"),
        path("vendor-bills/<uuid:bill_id>/analyze/", VendorBillAnalyzeView.as_view(), name="vendor-bill-analyze"),
        path("vendor-bills/<uuid:bill_id>/verify/", vendor_bill_verify_view, name="vendor-bill-verify"),
        path("vendor-bills/<uuid:bill_id>/sync/", VendorBillSyncView.as_view(), name="vendor-bill-sync"),

        # Expense Bills Workflow (Draft → Analyzed → Verified → Synced)
        path("expense-bills/", expense_bills_list_view, name="expense-bills-list"),
        path("expense-bills/upload/", expense_bills_upload_view, name="expense-bills-upload"),
        path("expense-bills/<uuid:bill_id>/", expense_bill_detail_view, name="expense-bill-detail"),
        path("expense-bills/<uuid:bill_id>/analyze/", ExpenseBillAnalyzeView.as_view(), name="expense-bill-analyze"),
        path("expense-bills/<uuid:bill_id>/verify/", expense_bill_verify_view, name="expense-bill-verify"),
        path("expense-bills/<uuid:bill_id>/sync/", ExpenseBillSyncView.as_view(), name="expense-bill-sync"),
    ])),
]

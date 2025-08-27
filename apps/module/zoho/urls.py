from django.urls import path

from apps.module.zoho.views.settings import (
    ZohoCredentialsView,
    GenerateTokenView,
    VendorListView, VendorSyncView,
    ChartOfAccountListView, ChartOfAccountSyncView,
    TaxesListView, TaxesSyncView,
    TDSTCSListView, TDSTCSSyncView,
    VendorCreditsListView, VendorCreditsSyncView,
    VendorGSTLookupView,
)
from apps.module.zoho.views.vendor_bills import (
    VendorBillListCreateView,
    VendorBillDetailView,
    VendorBillVerifyView,
    VendorBillSyncView,
)
from apps.module.zoho.views.expense_bills import (
    ExpenseBillListCreateView,
    ExpenseBillDetailView,
    ExpenseBillVerifyView,
    ExpenseBillSyncView,
)

app_name = "zoho"

urlpatterns = [
    # --- settings ---
    path("zoho/<uuid:org_id>/credentials/", ZohoCredentialsView.as_view(), name="credentials"),
    path("zoho/<uuid:org_id>/credentials/generate-token/", GenerateTokenView.as_view(), name="generate-token"),

    path("zoho/<uuid:org_id>/vendors/", VendorListView.as_view(), name="vendors-list"),
    path("zoho/<uuid:org_id>/vendors/sync/", VendorSyncView.as_view(), name="vendors-sync"),
    path("zoho/<uuid:org_id>/vendors/gst/", VendorGSTLookupView.as_view(), name="vendor-gst"),

    path("zoho/<uuid:org_id>/chart-of-accounts/", ChartOfAccountListView.as_view(), name="coa-list"),
    path("zoho/<uuid:org_id>/chart-of-accounts/sync/", ChartOfAccountSyncView.as_view(), name="coa-sync"),

    path("zoho/<uuid:org_id>/taxes/", TaxesListView.as_view(), name="taxes-list"),
    path("zoho/<uuid:org_id>/taxes/sync/", TaxesSyncView.as_view(), name="taxes-sync"),

    path("zoho/<uuid:org_id>/tds-tcs/", TDSTCSListView.as_view(), name="tds-tcs-list"),
    path("zoho/<uuid:org_id>/tds-tcs/sync/", TDSTCSSyncView.as_view(), name="tds-tcs-sync"),

    path("zoho/<uuid:org_id>/vendor-credits/", VendorCreditsListView.as_view(), name="vendor-credits-list"),
    path("zoho/<uuid:org_id>/vendor-credits/sync/", VendorCreditsSyncView.as_view(), name="vendor-credits-sync"),

    # --- vendor bills ---
    path("zoho/<uuid:org_id>/vendor-bills/", VendorBillListCreateView.as_view(), name="vendor-bill-list-create"),
    path("zoho/<uuid:org_id>/vendor-bills/<uuid:bill_id>/", VendorBillDetailView.as_view(), name="vendor-bill-detail"),
    path("zoho/<uuid:org_id>/vendor-bills/<uuid:bill_id>/verify/", VendorBillVerifyView.as_view(), name="vendor-bill-verify"),
    path("zoho/<uuid:org_id>/vendor-bills/<uuid:bill_id>/sync/", VendorBillSyncView.as_view(), name="vendor-bill-sync"),

    # --- expense bills ---
    path("zoho/<uuid:org_id>/expense-bills/", ExpenseBillListCreateView.as_view(), name="expense-bill-list-create"),
    path("zoho/<uuid:org_id>/expense-bills/<uuid:bill_id>/", ExpenseBillDetailView.as_view(), name="expense-bill-detail"),
    path("zoho/<uuid:org_id>/expense-bills/<uuid:bill_id>/verify/", ExpenseBillVerifyView.as_view(), name="expense-bill-verify"),
    path("zoho/<uuid:org_id>/expense-bills/<uuid:bill_id>/sync/", ExpenseBillSyncView.as_view(), name="expense-bill-sync"),
]

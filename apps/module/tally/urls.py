from django.urls import path

from apps.module.tally.views.tcp import (
    LedgerViewSet,
    MasterAPIView,
    TallyExpenseApi,
    TallyVendor,
)
from apps.module.tally.views.vendor_expense import (
    VendorBillUploadView,
    VendorBillListView,
    VendorBillDetailView,
    VendorBillForceAnalyzeView,
    VendorBillVerifyView,
    VendorBillSyncView,
    VendorBillAnalyzedDetailView,
    VendorBillAnalyzedUpdateView,
    ExpenseBillUploadView,
    ExpenseBillListView,
    ExpenseBillDetailView,
    ExpenseBillForceAnalyzeView,
    ExpenseBillVerifyView,
    ExpenseBillSyncView,
    ExpenseBillAnalyzedDetailView,
    ExpenseBillAnalyzedUpdateView,
)

app_name = "tally_tcp"

urlpatterns = [
    # Ledgers (bulk create)
    path("<uuid:org_id>/ledgers/", LedgerViewSet.as_view({"post": "create"}), name="tcp-ledgers"),

    # Product master dump receiver
    path("<uuid:org_id>/master/", MasterAPIView.as_view(), name="tcp-master"),

    # Expense (journal) synced export + intake
    path("<uuid:org_id>/expense-bills/", TallyExpenseApi.as_view(), name="tcp-expense"),

    # Vendor bills synced export + intake
    path("<uuid:org_id>/vendor-bills/", TallyVendor.as_view(), name="tcp-vendor"),

    # Vendor bills
    path("<uuid:org_id>/vendor-bills/upload/", VendorBillUploadView.as_view(), name="vendor-upload"),
    path("<uuid:org_id>/vendor-bills/", VendorBillListView.as_view(), name="vendor-list"),
    path("<uuid:org_id>/vendor-bills/<uuid:bill_id>/", VendorBillDetailView.as_view(), name="vendor-detail"),
    path("<uuid:org_id>/vendor-bills/<uuid:bill_id>/analyze/", VendorBillForceAnalyzeView.as_view(),
         name="vendor-analyze"),
    path("<uuid:org_id>/vendor-bills/<uuid:bill_id>/verify/", VendorBillVerifyView.as_view(),
         name="vendor-verify"),
    path("<uuid:org_id>/vendor-bills/<uuid:bill_id>/sync/", VendorBillSyncView.as_view(), name="vendor-sync"),
    path("<uuid:org_id>/vendor-bills/<uuid:bill_id>/analyzed/", VendorBillAnalyzedDetailView.as_view(),
         name="vendor-analyzed-detail"),
    path("<uuid:org_id>/vendor-bills/<uuid:bill_id>/analyzed/update/", VendorBillAnalyzedUpdateView.as_view(),
         name="vendor-analyzed-update"),

    # Expense bills
    path("<uuid:org_id>/expense-bills/upload/", ExpenseBillUploadView.as_view(), name="expense-upload"),
    path("<uuid:org_id>/expense-bills/", ExpenseBillListView.as_view(), name="expense-list"),
    path("<uuid:org_id>/expense-bills/<uuid:bill_id>/", ExpenseBillDetailView.as_view(), name="expense-detail"),
    path("<uuid:org_id>/expense-bills/<uuid:bill_id>/analyze/", ExpenseBillForceAnalyzeView.as_view(),
         name="expense-analyze"),
    path("<uuid:org_id>/expense-bills/<uuid:bill_id>/verify/", ExpenseBillVerifyView.as_view(),
         name="expense-verify"),
    path("<uuid:org_id>/expense-bills/<uuid:bill_id>/sync/", ExpenseBillSyncView.as_view(),
         name="expense-sync"),
    path("<uuid:org_id>/expense-bills/<uuid:bill_id>/analyzed/", ExpenseBillAnalyzedDetailView.as_view(),
         name="expense-analyzed-detail"),
    path("<uuid:org_id>/expense-bills/<uuid:bill_id>/analyzed/update/", ExpenseBillAnalyzedUpdateView.as_view(),
         name="expense-analyzed-update"),
]

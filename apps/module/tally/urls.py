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
    VendorBillForceAnalyzeView,
    VendorBillVerifyView,
    VendorBillSyncView,
    ExpenseBillUploadView,
    ExpenseBillListView,
    ExpenseBillForceAnalyzeView,
    ExpenseBillVerifyView,
    ExpenseBillSyncView,
)

app_name = "tally_tcp"

urlpatterns = [
    # Ledgers (bulk create)
    path("tally/org/<uuid:org_id>/ledgers/", LedgerViewSet.as_view({"post": "create"}), name="tcp-ledgers"),

    # Product master dump receiver
    path("tally/org/<uuid:org_id>/master/", MasterAPIView.as_view(), name="tcp-master"),

    # Expense (journal) synced export + intake
    path("tally/org/<uuid:org_id>/expense-bills/", TallyExpenseApi.as_view(), name="tcp-expense"),

    # Vendor bills synced export + intake
    path("tally/org/<uuid:org_id>/vendor-bills/", TallyVendor.as_view(), name="tcp-vendor"),

    # Vendor bills
    path("tally/org/<uuid:org_id>/vendor-bills/upload/", VendorBillUploadView.as_view(), name="vendor-upload"),
    path("tally/org/<uuid:org_id>/vendor-bills/", VendorBillListView.as_view(), name="vendor-list"),
    path("tally/org/<uuid:org_id>/vendor-bills/<uuid:bill_id>/analyze/", VendorBillForceAnalyzeView.as_view(),
         name="vendor-analyze"),
    path("tally/org/<uuid:org_id>/vendor-bills/<uuid:bill_id>/verify/", VendorBillVerifyView.as_view(),
         name="vendor-verify"),
    path("tally/org/<uuid:org_id>/vendor-bills/<uuid:bill_id>/sync/", VendorBillSyncView.as_view(), name="vendor-sync"),

    # Expense bills
    path("tally/org/<uuid:org_id>/expense-bills/upload/", ExpenseBillUploadView.as_view(), name="expense-upload"),
    path("tally/org/<uuid:org_id>/expense-bills/", ExpenseBillListView.as_view(), name="expense-list"),
    path("tally/org/<uuid:org_id>/expense-bills/<uuid:bill_id>/analyze/", ExpenseBillForceAnalyzeView.as_view(),
         name="expense-analyze"),
    path("tally/org/<uuid:org_id>/expense-bills/<uuid:bill_id>/verify/", ExpenseBillVerifyView.as_view(),
         name="expense-verify"),
    path("tally/org/<uuid:org_id>/expense-bills/<uuid:bill_id>/sync/", ExpenseBillSyncView.as_view(),
         name="expense-sync"),
]

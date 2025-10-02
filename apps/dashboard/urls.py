from django.urls import path
from .views import (
    ZohoOverviewView, ZohoTimeseriesView, ZohoFunnelView, ZohoTopVendorsView,
    ZohoTaxesSummaryView, ZohoExpenseSummaryView, ZohoPendingView,
    ZohoIntegrationsHealthView, ZohoUsageView
)

app_name = "dashboard"

urlpatterns = [
    # Zoho Dashboard Analytics - Organization Scoped
    path('organizations/<uuid:org_id>/zoho/overview/', ZohoOverviewView.as_view(), name='zoho_overview'),
    path('organizations/<uuid:org_id>/zoho/funnel/', ZohoFunnelView.as_view(), name='zoho_funnel'),
    path('organizations/<uuid:org_id>/zoho/usage/', ZohoUsageView.as_view(), name='zoho_usage'),
]

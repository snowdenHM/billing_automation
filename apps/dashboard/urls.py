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
    path('organizations/<uuid:org_id>/zoho/timeseries/', ZohoTimeseriesView.as_view(), name='zoho_timeseries'),
    path('organizations/<uuid:org_id>/zoho/funnel/', ZohoFunnelView.as_view(), name='zoho_funnel'),
    path('organizations/<uuid:org_id>/zoho/vendors/top/', ZohoTopVendorsView.as_view(), name='zoho_top_vendors'),
    path('organizations/<uuid:org_id>/zoho/taxes/summary/', ZohoTaxesSummaryView.as_view(), name='zoho_taxes_summary'),
    path('organizations/<uuid:org_id>/zoho/expenses/summary/', ZohoExpenseSummaryView.as_view(), name='zoho_expense_summary'),
    path('organizations/<uuid:org_id>/zoho/pending/', ZohoPendingView.as_view(), name='zoho_pending'),
    path('organizations/<uuid:org_id>/zoho/integrations/health/', ZohoIntegrationsHealthView.as_view(), name='zoho_integrations_health'),
    path('organizations/<uuid:org_id>/zoho/usage/', ZohoUsageView.as_view(), name='zoho_usage'),
]

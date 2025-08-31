from django.urls import path
from .views import (
    OverviewView, TimeseriesView, FunnelView, TopVendorsView,
    TaxesSummaryView, ExpenseSummaryView, PendingView,
    IntegrationsHealthView, UsageView
)

app_name = "dashboard"

urlpatterns = [
    path("<uuid:org_id>/overview/", OverviewView.as_view(), name="overview"),
    path("<uuid:org_id>/timeseries/", TimeseriesView.as_view(), name="timeseries"),
    path("<uuid:org_id>/funnel/", FunnelView.as_view(), name="funnel"),
    path("<uuid:org_id>/top-vendors/", TopVendorsView.as_view(), name="top_vendors"),
    path("<uuid:org_id>/taxes/summary/", TaxesSummaryView.as_view(), name="taxes_summary"),
    path("<uuid:org_id>/expenses/summary/", ExpenseSummaryView.as_view(), name="expenses_summary"),
    path("<uuid:org_id>/pending/", PendingView.as_view(), name="pending"),
    path("<uuid:org_id>/integrations/health/", IntegrationsHealthView.as_view(), name="integrations_health"),
    path("<uuid:org_id>/usage/", UsageView.as_view(), name="usage"),
]

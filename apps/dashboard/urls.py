from django.urls import path
from .views import (
    ZohoOverviewView, ZohoFunnelView, ZohoUsageView,
    TallyOverviewView, TallyFunnelView, TallyUsageView
)

app_name = "dashboard"

urlpatterns = [
    # Zoho Dashboard Analytics - Organization Scoped
    path('organizations/<uuid:org_id>/zoho/overview/', ZohoOverviewView.as_view(), name='zoho_overview'),
    path('organizations/<uuid:org_id>/zoho/funnel/', ZohoFunnelView.as_view(), name='zoho_funnel'),
    path('organizations/<uuid:org_id>/zoho/usage/', ZohoUsageView.as_view(), name='zoho_usage'),

    # Tally Dashboard Analytics - Organization Scoped
    path('organizations/<uuid:org_id>/tally/overview/', TallyOverviewView.as_view(), name='tally_overview'),
    path('organizations/<uuid:org_id>/tally/funnel/', TallyFunnelView.as_view(), name='tally_funnel'),
    path('organizations/<uuid:org_id>/tally/usage/', TallyUsageView.as_view(), name='tally_usage'),
]

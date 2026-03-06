from django.urls import path
from .views import (
    zoho_overview_view, zoho_funnel_view, zoho_usage_view,
    tally_overview_view, tally_funnel_view, tally_usage_view,
)

app_name = "dashboard"

urlpatterns = [
    # Zoho Dashboard Analytics - Organization Scoped
    path('organizations/<uuid:org_id>/zoho/overview/', zoho_overview_view, name='zoho_overview'),
    path('organizations/<uuid:org_id>/zoho/funnel/', zoho_funnel_view, name='zoho_funnel'),
    path('organizations/<uuid:org_id>/zoho/usage/', zoho_usage_view, name='zoho_usage'),

    # Tally Dashboard Analytics - Organization Scoped
    path('organizations/<uuid:org_id>/tally/overview/', tally_overview_view, name='tally_overview'),
    path('organizations/<uuid:org_id>/tally/funnel/', tally_funnel_view, name='tally_funnel'),
    path('organizations/<uuid:org_id>/tally/usage/', tally_usage_view, name='tally_usage'),
]

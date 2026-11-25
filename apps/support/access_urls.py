"""Routes that do not hang off ``support/``.

The grant list and the access banner are *organization* endpoints — the
client reads them about their own account — and the desk is its own tree.
Mounted at the root of ``/api/v1/`` from ``apps.api.urls``.
"""
from django.urls import path

from . import access_views

app_name = "support_access"

urlpatterns = [
    # Client side
    path(
        "organizations/<uuid:org_id>/access-grants/",
        access_views.organization_access_grants_view,
        name="org-access-grants",
    ),
    path(
        "organizations/<uuid:org_id>/access-status/",
        access_views.organization_access_status_view,
        name="org-access-status",
    ),

    # Support desk
    path("support-desk/tickets/", access_views.desk_ticket_list_view, name="desk-tickets"),
    path(
        "support-desk/tickets/<uuid:ticket_id>/",
        access_views.desk_ticket_detail_view,
        name="desk-ticket-detail",
    ),
    path(
        "support-desk/tickets/<uuid:ticket_id>/reply/",
        access_views.desk_ticket_reply_view,
        name="desk-ticket-reply",
    ),
    path(
        "support-desk/tickets/<uuid:ticket_id>/assign/",
        access_views.desk_ticket_assign_view,
        name="desk-ticket-assign",
    ),
    path(
        "support-desk/access/redeem/",
        access_views.desk_access_redeem_view,
        name="desk-access-redeem",
    ),
    path("support-desk/access/end/", access_views.desk_access_end_view, name="desk-access-end"),
    path(
        "support-desk/access/sessions/",
        access_views.desk_access_sessions_view,
        name="desk-access-sessions",
    ),
]

# TO WIRE UP: add the following line to apps/api/urls.py's urlpatterns list:
#     path("support/", include("apps.support.urls")),

from django.urls import path

from . import access_views, views

app_name = "support"

urlpatterns = [
    path("tickets/", views.support_ticket_create_view, name="ticket-create"),
    path("chat/", views.support_chat_view, name="chat"),
    path("tickets/mine/", views.support_ticket_mine_view, name="ticket-mine"),
    path("tickets/<uuid:pk>/reply/", views.support_ticket_reply_view, name="ticket-reply"),

    # Consent-gated access, client side. Minting lives under the ticket
    # because a grant without a ticket has no reason attached to it, and
    # no closing event to end it.
    path(
        "tickets/<uuid:ticket_id>/access-grants/",
        access_views.mint_access_grant_view,
        name="access-grant-mint",
    ),
    path(
        "access-grants/<uuid:grant_id>/revoke/",
        access_views.revoke_access_grant_view,
        name="access-grant-revoke",
    ),
]

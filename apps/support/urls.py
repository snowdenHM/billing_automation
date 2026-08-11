# TO WIRE UP: add the following line to apps/api/urls.py's urlpatterns list:
#     path("support/", include("apps.support.urls")),

from django.urls import path

from . import views

app_name = "support"

urlpatterns = [
    path("tickets/", views.support_ticket_create_view, name="ticket-create"),
    path("tickets/mine/", views.support_ticket_mine_view, name="ticket-mine"),
    path("tickets/<uuid:pk>/reply/", views.support_ticket_reply_view, name="ticket-reply"),
]

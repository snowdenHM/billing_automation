"""Public (unauthenticated) endpoints served from the common app."""

from django.urls import path

from apps.common.views import (
    book_demo_verify_view,
    book_demo_view,
    maintenance_status_view,
)

app_name = "common"

urlpatterns = [
    path("book-demo/", book_demo_view, name="book-demo"),
    path("book-demo/verify/", book_demo_verify_view, name="book-demo-verify"),
    # Deliberately under /maintenance/, which MaintenanceModeMiddleware
    # always lets through — a status endpoint that goes down with the
    # thing it reports on is useless.
    path("maintenance/", maintenance_status_view, name="maintenance-status"),
]

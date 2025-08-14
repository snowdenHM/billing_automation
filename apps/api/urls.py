from django.urls import path, include

urlpatterns = [
    path("", include("apps.users.urls", namespace="users")),
    path("", include("apps.organizations.urls")),
    path("", include("apps.subscriptions.urls")),
    path("", include("apps.module.zoho.urls")),
    path("", include("apps.module.tally.urls")),
    path("dashboard/", include("apps.dashboard.urls", namespace="dashboard")),

]

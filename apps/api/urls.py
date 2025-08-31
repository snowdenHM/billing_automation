from django.urls import path, include

urlpatterns = [
    path("users/", include("apps.users.urls", namespace="users")),
    path("", include("apps.organizations.urls")),
    path("plan/", include("apps.subscriptions.urls")),
    path("zoho/", include("apps.module.zoho.urls")),
    path("tally/", include("apps.module.tally.urls")),
    path("dashboard/", include("apps.dashboard.urls", namespace="dashboard")),

]

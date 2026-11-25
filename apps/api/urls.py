from django.urls import path, include

urlpatterns = [
    path("", include("apps.common.urls", namespace="common")),
    path("", include("apps.users.urls", namespace="users")),
    path("", include("apps.organizations.urls")),
    path("", include("apps.subscriptions.urls")),
    path("zoho/", include("apps.module.zoho.urls")),
    path("tally/", include("apps.module.tally.urls")),
    path("", include("apps.dashboard.urls", namespace="dashboard")),
    path("support/", include("apps.support.urls")),
    # The access-grant and support-desk trees sit at the root rather than
    # under support/: the grant list is an organization endpoint the
    # client reads about their own account.
    path("", include("apps.support.access_urls")),

]

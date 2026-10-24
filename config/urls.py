"""
URL Configuration for Bill Munshi project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/4.2/topics/http/urls/
"""

from django.contrib import admin
from django.urls import path, include, re_path
from django.conf import settings
from django.conf.urls.static import static
from django.views.generic import RedirectView
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView, SpectacularRedocView
from apps.common.views import serve_bill_file

# Main URL patterns
urlpatterns = [
    # The API host has no front door of its own. It used to serve a second
    # marketing page here, which meant two pages to keep true and two
    # places a claim could go stale. The React app is the product's
    # landing page; anyone who lands on the API host is sent there.
    #
    # 302 rather than 301: a permanent redirect is cached by browsers
    # more or less forever, so moving this later would mean chasing it
    # out of caches we do not control.
    path(
        "",
        RedirectView.as_view(url=settings.FRONTEND_URL.rstrip("/") or "/admin/",
                             permanent=False),
        name="landing",
    ),

    # Admin
    path("admin/", admin.site.urls),

    # API v1 endpoints
    path("api/v1/", include("apps.api.urls")),

    # Background job queue dashboard
    path("admin/rq/", include("django_rq.urls")),
]

# API documentation — development only.
#
# These three served the complete schema to anyone who asked: every
# endpoint, every field name, every request and response shape, the
# voucher types, the role names. For a competitor that is the product's
# blueprint; for an attacker it is the attack surface, written out.
#
# The Tally connector's contract lives in docs/ as Markdown, which is
# what the integrator actually needs, so nothing outside the team loses
# anything by this being closed.
if settings.DEBUG:
    urlpatterns += [
        path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
        path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"),
             name="swagger-ui"),
        path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"),
             name="redoc"),
    ]

# Media is always served through serve_bill_file, which verifies the signed
# token the serializers mint. Registering this only under DEBUG left the
# guard dead in production: /media/ either 404'd or, if the web server
# aliased it to MEDIA_ROOT, served every tenant's bills with no auth at all.
urlpatterns += [
    re_path(r'^media/(?P<path>.*)$', serve_bill_file, name='serve_media_file'),
]

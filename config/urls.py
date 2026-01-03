"""
URL Configuration for Bill Munshi project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/4.2/topics/http/urls/
"""

from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.views.generic import TemplateView
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView, SpectacularRedocView

# Main URL patterns
urlpatterns = [
    # Landing page
    path("", TemplateView.as_view(template_name="index.html"), name="landing"),

    # Django Admin
    path("admin/", admin.site.urls),

    # API v1 endpoints
    path("api/v1/", include("apps.api.urls")),

    # API Documentation
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
    
    # Django-RQ admin interface
    path("admin/rq/", include("django_rq.urls")),
]

# Serve media files in development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

# TO WIRE UP: add 'apps.support' to LOCAL_APPS in config/settings/base.py
from django.apps import AppConfig


class SupportConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.support"

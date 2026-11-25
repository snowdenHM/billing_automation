# TO WIRE UP: add 'apps.support' to LOCAL_APPS in config/settings/base.py
from django.apps import AppConfig


class SupportConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.support"

    def ready(self):
        # Imported for the side effect of registering the ticket
        # lifecycle receivers — closing a ticket has to revoke its grants
        # whoever closed it, including the Django admin.
        from . import signals  # noqa: F401

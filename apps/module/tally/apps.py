from django.apps import AppConfig


class TallyConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.module.tally'

    def ready(self):
        from . import signals  # noqa: F401  (registers post_save receiver)
        return super().ready()

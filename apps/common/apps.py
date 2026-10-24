from django.apps import AppConfig
import logging

logger = logging.getLogger(__name__)


class CommonConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.common"

    def ready(self):
        """Apply patches when Django starts up."""
        try:
            # Import and apply the DRF model_meta patch
            from . import drf_patches
            logger.info("Applied DRF model_meta patch for ManyToMany field safety")
        except ImportError as e:
            logger.warning(f"Warning: Could not apply DRF patches: {e}")

        _rename_vendor_apps_in_admin()


# The admin sidebar groups models by app and prints each app's
# ``verbose_name``. Two dependencies ship their own branding in that string —
# "django-waffle" and "Django-RQ" — so the framework's name was on an
# operator's screen on every admin page, under a product that is not called
# that. These are the only two places it appeared; the rest of the admin is
# already branded through the UNFOLD settings.
#
# Renaming here rather than forking the packages: ``verbose_name`` is read at
# render time, so reassigning it once at startup is enough, and an upgrade of
# either package keeps the new label.
_VENDOR_APP_LABELS = {
    "waffle": "Feature Flags",
    "django_rq": "Background Jobs",
}

#: ``django_rq`` also names its one placeholder model "Django-RQ", which is
#: what the sidebar link itself says.
_VENDOR_MODEL_NAMES = {
    ("django_rq", "dashboard"): ("Job queue", "Job queues"),
}


def _rename_vendor_apps_in_admin():
    from django.apps import apps as django_apps

    for label, title in _VENDOR_APP_LABELS.items():
        try:
            django_apps.get_app_config(label).verbose_name = title
        except LookupError:
            # The dependency is not installed in this deployment.
            continue

    for (label, model_name), (singular, plural) in _VENDOR_MODEL_NAMES.items():
        try:
            opts = django_apps.get_model(label, model_name)._meta
        except LookupError:
            continue
        opts.verbose_name = singular
        opts.verbose_name_plural = plural

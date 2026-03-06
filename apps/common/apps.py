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

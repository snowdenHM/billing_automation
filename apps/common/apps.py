from django.apps import AppConfig


class CommonConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.common"

    def ready(self):
        """Apply patches when Django starts up."""
        try:
            # Import and apply the DRF model_meta patch
            from . import drf_patches
            print("Applied DRF model_meta patch for ManyToMany field safety")
        except ImportError as e:
            print(f"Warning: Could not apply DRF patches: {e}")

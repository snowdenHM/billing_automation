#!/usr/bin/env python
import os
import sys

def main():
    """
    Run administrative tasks.

    Priority for selecting settings:
    1) DJANGO_SETTINGS_MODULE (env)
    2) BILLMUNSHI_ENV / DJANGO_ENV = production|prod -> config.settings.production
       otherwise -> config.settings.local
    3) Fallback -> config.settings.local
    """
    # Load .env if available (no error if missing)
    try:
        from dotenv import load_dotenv  # python-dotenv
        load_dotenv()
    except Exception:
        pass

    settings_module = os.environ.get("DJANGO_SETTINGS_MODULE")
    if not settings_module:
        env = (os.environ.get("BILLMUNSHI_ENV")
               or os.environ.get("DJANGO_ENV")
               or "local").strip().lower()
        if env in ("prod", "production"):
            settings_module = "config.settings.production"
        else:
            settings_module = "config.settings.local"

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", settings_module)

    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Is it installed and available on your "
            "PYTHONPATH environment variable? Did you activate a virtualenv?"
        ) from exc

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()

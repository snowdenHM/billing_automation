"""
Django settings package for Bill Munshi project.

This package uses a split settings approach:
- base.py: Common settings for all environments
- local.py: Development environment settings
- production.py: Production environment settings

Settings are imported via environment variable DJANGO_SETTINGS_MODULE
which should point to the appropriate module (e.g., config.settings.production).
"""

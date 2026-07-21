"""
Runtime email config loader.

Wraps the DB-backed ``EmailSettings`` singleton with a tiny in-process
cache. The cache is invalidated whenever the row is saved (see
``EmailSettings.save``) or manually via ``invalidate_email_config``.

Callers get a plain :class:`EmailConfig` dataclass so the surface stays
independent of Django ORM concerns.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from django.conf import settings
from django.db import OperationalError, ProgrammingError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmailConfig:
    is_enabled: bool
    sendgrid_api_key: str
    from_email: str
    from_name: str
    reply_to: str

    @property
    def formatted_from(self) -> str:
        if self.from_name:
            return f"{self.from_name} <{self.from_email}>"
        return self.from_email

    @property
    def use_sendgrid(self) -> bool:
        return bool(self.is_enabled and self.sendgrid_api_key)


_CACHE: EmailConfig | None = None


def _default_from_settings() -> EmailConfig:
    """Build a config purely from Django settings — used as a safe fallback."""
    from_email_raw = getattr(settings, "DEFAULT_FROM_EMAIL", "support@billmunshi.com")
    # DEFAULT_FROM_EMAIL may be "Name <email>". Split naïvely.
    name = ""
    address = from_email_raw
    if "<" in from_email_raw and ">" in from_email_raw:
        name = from_email_raw.split("<", 1)[0].strip().strip('"')
        address = from_email_raw.split("<", 1)[1].rstrip(">").strip()

    env_key = getattr(settings, "SENDGRID_API_KEY", "") or ""

    return EmailConfig(
        is_enabled=True,
        sendgrid_api_key=env_key,
        from_email=address or "support@billmunshi.com",
        from_name=name or "Bill Munshi",
        reply_to=address or "support@billmunshi.com",
    )


def get_email_config() -> EmailConfig:
    """Return the current email config, cached in-process."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    try:
        # Local import so `apps.common.email_config` stays importable
        # during app-loading before models are ready (e.g. Django checks).
        from apps.common.models import EmailSettings

        row = EmailSettings.load()
        _CACHE = EmailConfig(
            is_enabled=row.is_enabled,
            sendgrid_api_key=row.sendgrid_api_key or "",
            from_email=row.from_email,
            from_name=row.from_name,
            reply_to=row.reply_to or row.from_email,
        )
    except (OperationalError, ProgrammingError) as exc:
        # DB not yet migrated (e.g. running `manage.py migrate` itself).
        # Fall back to settings so send attempts never hard-fail here.
        logger.warning("EmailSettings unavailable, using settings fallback: %s", exc)
        _CACHE = _default_from_settings()
    return _CACHE


def invalidate_email_config() -> None:
    """Drop the cached config; the next ``get_email_config`` call reloads."""
    global _CACHE
    _CACHE = None

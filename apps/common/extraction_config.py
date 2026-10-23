"""Runtime extraction config: the admin row, falling back to the environment.

The bill-reading route used to be environment variables, so trying a
different one meant a redeploy and rolling one back meant another. These
are settings somebody wants to flip while watching what happens, so they
moved into a singleton an admin can edit.

Two rules make the move safe:

* **Empty means inherit.** A field left blank on the row defers to the
  environment variable it replaced, so a fresh install behaves exactly
  as it did before the table existed, and clearing a field hands control
  back to the deployment.

* **A missing table is not an outage.** Before the migration runs, or if
  the database is unreachable, every lookup falls through to settings.
  Bill extraction must not stop because a config row could not be read.

Cached, because this is read on every bill and a query per call would be
a query per bill for no reason. The cache lives in the **shared** cache
backend rather than a module global, and that detail matters: bills are
analysed in the RQ worker, not the web process. A module-level cache
would mean an operator flipping a switch in admin changed the web
process and nothing else, while the worker — the only process that
actually reads bills — carried on with the old value until it restarted.

A short TTL backs the explicit invalidation up, so a process that misses
the delete still converges within half a minute.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.conf import settings
from django.core.cache import cache
from django.db import OperationalError, ProgrammingError

logger = logging.getLogger(__name__)

_CACHE_KEY = "extraction_config:v1"
_CACHE_TTL = 30  # seconds


@dataclass(frozen=True)
class ExtractionConfig:
    ocr_first: bool
    ocr_assist: bool
    ocr_shadow: bool
    split_batches: bool
    vision_model: str
    text_model: str
    vision_detail: str
    ocr_min_confidence: float
    ocr_min_chars: int
    rollout_org_ids: frozenset = field(default_factory=frozenset)

    def ocr_first_for(self, organization_id) -> bool:
        """Is the OCR-first route on for this organisation?

        Global switch, or this organisation on the rollout list — so a
        route can be proven on one client before it reaches the rest.
        """
        if self.ocr_first:
            return True
        return bool(organization_id) and str(organization_id) in self.rollout_org_ids


def _from_settings() -> ExtractionConfig:
    """The environment's own answer, used as both fallback and default."""
    return ExtractionConfig(
        ocr_first=bool(getattr(settings, "EXTRACTION_OCR_FIRST", False)),
        ocr_assist=bool(getattr(settings, "EXTRACTION_OCR_ASSIST", False)),
        ocr_shadow=bool(getattr(settings, "EXTRACTION_OCR_SHADOW", False)),
        split_batches=bool(getattr(settings, "EXTRACTION_SPLIT_BATCHES", False)),
        vision_model=getattr(settings, "EXTRACTION_VISION_MODEL", "gpt-4o"),
        text_model=getattr(settings, "EXTRACTION_TEXT_MODEL", "gpt-4o-mini"),
        vision_detail=getattr(settings, "EXTRACTION_VISION_DETAIL", "high"),
        ocr_min_confidence=float(getattr(settings, "EXTRACTION_OCR_MIN_CONFIDENCE", 0.90)),
        ocr_min_chars=int(getattr(settings, "EXTRACTION_OCR_MIN_CHARS", 200)),
        rollout_org_ids=frozenset(
            str(x) for x in (getattr(settings, "EXTRACTION_OCR_ORG_IDS", None) or [])
        ),
    )


def _pick(row_value, env_value):
    """Row wins unless it is empty — empty means "inherit"."""
    if row_value is None or row_value == "":
        return env_value
    return row_value


def get_extraction_config() -> ExtractionConfig:
    """Current config. Cached; call :func:`invalidate_extraction_config` after a write."""
    try:
        cached = cache.get(_CACHE_KEY)
        if cached is not None:
            return cached
    except Exception:
        # A cache that is down must not stop a bill being read.
        logger.debug("extraction config cache unavailable; reading the row")

    env = _from_settings()
    try:
        from apps.common.models import ExtractionSettings

        row = ExtractionSettings.objects.prefetch_related("rollout_organizations").first()
        if row is None:
            resolved = env
        else:
            rollout = frozenset(
                str(o.id) for o in row.rollout_organizations.all()
            ) or env.rollout_org_ids
            resolved = ExtractionConfig(
                ocr_first=bool(_pick(row.ocr_first, env.ocr_first)),
                ocr_assist=bool(_pick(row.ocr_assist, env.ocr_assist)),
                ocr_shadow=bool(_pick(row.ocr_shadow, env.ocr_shadow)),
                split_batches=bool(_pick(row.split_batches, env.split_batches)),
                vision_model=_pick(row.vision_model, env.vision_model),
                text_model=_pick(row.text_model, env.text_model),
                vision_detail=_pick(row.vision_detail, env.vision_detail),
                ocr_min_confidence=float(
                    _pick(row.ocr_min_confidence, env.ocr_min_confidence)),
                ocr_min_chars=int(_pick(row.ocr_min_chars, env.ocr_min_chars)),
                rollout_org_ids=rollout,
            )
    except (OperationalError, ProgrammingError):
        # Table not migrated yet, or the database is unreachable. Reading
        # a config row is not worth failing a bill over.
        logger.debug("ExtractionSettings unavailable; using environment values")
        resolved = env
    except Exception:
        logger.exception("Could not read ExtractionSettings; using environment values")
        resolved = env

    try:
        cache.set(_CACHE_KEY, resolved, _CACHE_TTL)
    except Exception:
        logger.debug("could not cache extraction config")
    return resolved


def invalidate_extraction_config() -> None:
    """Drop the cache for every process. Called from ``ExtractionSettings.save``.

    Shared, so the RQ worker sees the change too — it is the process
    that actually reads bills, and an admin toggle that only reached the
    web process would look like it had done nothing.
    """
    try:
        cache.delete(_CACHE_KEY)
    except Exception:
        logger.warning(
            "Could not clear the extraction config cache; processes will "
            "pick the change up within %ss.", _CACHE_TTL,
        )

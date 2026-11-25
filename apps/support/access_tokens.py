"""Minting and verifying the spoken access code.

The code is read out on a phone call, so its alphabet is Crockford base32
minus ``I L O U`` — no character that is heard, or written down, as another
one. Twelve characters is 32^12 (~2^60); with a 60-minute TTL and the
lockout below, guessing is not a live threat.

Nothing here logs or returns the plaintext twice. ``mint_code`` hands it
back once, to the one caller that answers the client admin's mint request;
after that only ``sha256(code)`` exists anywhere.
"""
import hashlib
import hmac
import logging
import secrets

from django.utils import timezone

logger = logging.getLogger(__name__)

# Crockford base32 minus I, L, O, U.
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

CODE_PREFIX = "BM"
GROUP_SIZE = 4
GROUPS = 3  # 12 secret characters, in groups of four

# 5 failed redemptions per agent per 15 minutes, then a 15-minute lockout.
MAX_FAILED_ATTEMPTS = 5
ATTEMPT_WINDOW_SECONDS = 15 * 60
LOCKOUT_SECONDS = 15 * 60


class RedemptionLocked(Exception):
    """Raised when an agent has burned through their failed attempts."""

    def __init__(self, retry_after_seconds):
        self.retry_after_seconds = retry_after_seconds
        super().__init__("Too many failed attempts.")


def _attempt_key(agent_id):
    return f"support-access:redeem-fail:{agent_id}"


def _lockout_key(agent_id):
    return f"support-access:redeem-lock:{agent_id}"


def mint_code():
    """Return ``(code, code_label, token_hash)``.

    The caller must hand ``code`` straight to the client admin who asked
    for it and keep no copy — it is not recoverable afterwards.
    """
    groups = [
        "".join(secrets.choice(ALPHABET) for _ in range(GROUP_SIZE))
        for _ in range(GROUPS)
    ]
    code = "-".join([CODE_PREFIX] + groups)
    # The label is the prefix plus the first group only, which is what both
    # sides say out loud to agree *which* grant they mean. The remaining
    # eight characters are the part that must never appear on a screen,
    # in a log line or in a support email.
    code_label = f"{CODE_PREFIX}-{groups[0]}"
    return code, code_label, hash_code(code)


def normalize_code(raw):
    """Fold a spoken-then-typed code back to canonical form.

    People type it lowercase, with spaces, or without the dashes. None of
    that changes which grant they meant, so none of it should fail.
    """
    cleaned = "".join(ch for ch in str(raw or "").upper() if ch.isalnum())
    if not cleaned.startswith(CODE_PREFIX):
        return ""
    body = cleaned[len(CODE_PREFIX):]
    if len(body) != GROUP_SIZE * GROUPS or any(ch not in ALPHABET for ch in body):
        return ""
    groups = [body[i:i + GROUP_SIZE] for i in range(0, len(body), GROUP_SIZE)]
    return "-".join([CODE_PREFIX] + groups)


def hash_code(code):
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def check_lockout(agent_id):
    """Raise ``RedemptionLocked`` if this agent is in a lockout window."""
    from django.core.cache import cache

    try:
        locked_until = cache.get(_lockout_key(agent_id))
    except Exception:  # pragma: no cover - cache outage
        logger.warning("Redemption lockout cache unavailable; allowing attempt.")
        return
    if not locked_until:
        return
    remaining = int((locked_until - timezone.now()).total_seconds())
    if remaining <= 0:
        cache.delete(_lockout_key(agent_id))
        return
    raise RedemptionLocked(remaining)


def record_failure(agent_id):
    """Count one failed redemption, and start a lockout at the limit.

    Failures are logged with the agent's id and never with the code they
    typed: a wrong code is often one character off a right one.
    """
    from django.core.cache import cache

    key = _attempt_key(agent_id)
    try:
        if cache.add(key, 1, timeout=ATTEMPT_WINDOW_SECONDS):
            count = 1
        else:
            count = cache.incr(key)
        logger.warning(
            "Support access redemption failed for agent %s (%s/%s in window).",
            agent_id, count, MAX_FAILED_ATTEMPTS,
        )
        if count >= MAX_FAILED_ATTEMPTS:
            cache.set(
                _lockout_key(agent_id),
                timezone.now() + timezone.timedelta(seconds=LOCKOUT_SECONDS),
                timeout=LOCKOUT_SECONDS,
            )
            cache.delete(key)
            logger.warning(
                "Support access redemption locked out for agent %s.", agent_id
            )
    except Exception:  # pragma: no cover - cache outage
        logger.warning("Redemption attempt cache unavailable for agent %s.", agent_id)


def clear_failures(agent_id):
    from django.core.cache import cache

    try:
        cache.delete(_attempt_key(agent_id))
        cache.delete(_lockout_key(agent_id))
    except Exception:  # pragma: no cover - cache outage
        pass


def find_grant_for_code(raw_code):
    """The live grant this code opens, or ``None``.

    The hash lookup is indexed and the comparison is
    ``hmac.compare_digest`` — a plain ``==`` on a hex digest leaks, through
    its timing, how far down the string the attacker got right, which turns
    a 2^60 search into a character-by-character one.

    Usability is judged by ``is_usable()``, i.e. against the clock, so a
    grant past its TTL whose status column still reads "active" is refused
    here too.
    """
    from .models import SupportAccessGrant

    code = normalize_code(raw_code)
    if not code:
        return None

    digest = hash_code(code)
    # Narrowed by the index, then confirmed in constant time. Postgres
    # found the row by an equality the attacker cannot observe; what they
    # *can* observe is this comparison.
    for grant in SupportAccessGrant.objects.filter(token_hash=digest).select_related(
        "ticket", "organization"
    ):
        if hmac.compare_digest(grant.token_hash, digest) and grant.is_usable():
            return grant
    return None

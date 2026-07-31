"""Collision-free default names for uploaded bills.

Every bill model generates a display name of the form
``<YYYYMMDD><CODE><00001>`` (e.g. ``20260726ZB00001``) scoped to one
organisation and one calendar day.

The original implementation lived inline in each of the six bill models and
computed the next number as "read the current maximum, add one". That is a
read-modify-write with no locking and no uniqueness constraint behind it:
two uploads that interleave between the SELECT and the INSERT both observe
the same maximum and both persist the same name. Uploading several files at
once — the common case — is exactly the workload that triggers it, and once
a duplicate is written nothing ever detects or repairs it.

The fix has two halves, and both are required:

* a ``UniqueConstraint`` on ``(organization, <name field>)`` so the database
  is the final arbiter — no interleaving can produce two identical names;
* :func:`save_with_unique_name`, which retries on the resulting
  ``IntegrityError`` with a freshly computed number, so a losing writer
  quietly takes the next slot instead of surfacing an error to the user.

The retry is wrapped in a savepoint. Without it, an ``IntegrityError``
raised inside a caller's ``transaction.atomic()`` block would poison the
whole transaction and make the retry impossible.
"""

import logging
import re
from datetime import date, datetime, timezone

from django.db import IntegrityError, transaction

logger = logging.getLogger(__name__)

# Enough headroom to absorb a burst of concurrent uploads; each retry only
# costs one indexed SELECT plus a failed INSERT.
MAX_NAME_ATTEMPTS = 10

# Bills created from this instant on must have unique names; anything older is
# exempt.
#
# The uniqueness constraint is deliberately *partial*. Bills already in the
# database were written by the buggy generator and some of them collide. A
# plain constraint could only be added by renaming those rows first — an
# irreversible edit to historical records that users may have referenced
# elsewhere. Scoping the constraint by creation time leaves that history
# exactly as it is while still guaranteeing every new bill is unique.
#
# Same-day uploads are the only ones that can collide (names are date
# prefixed), so restricting enforcement to new rows loses no protection in
# practice.
#
# Raise this to your deploy date if you deploy well after this was written;
# see the accompanying migrations, which only repair rows at or after it.
NAME_UNIQUENESS_ENFORCED_FROM = datetime(2026, 7, 31, tzinfo=timezone.utc)


def build_bill_prefix(code, today=None):
    """Return the ``<YYYYMMDD><CODE>`` prefix for a bill name."""
    return f"{(today or date.today()).strftime('%Y%m%d')}{code}"


def next_bill_name(model, organization, code, name_field, today=None):
    """Return the next unused ``<YYYYMMDD><CODE><NNNNN>`` name.

    Scans only names already carrying today's prefix for this organisation,
    so the counter restarts at 00001 each day.
    """
    prefix = build_bill_prefix(code, today)

    existing = model.objects.filter(
        organization=organization,
        **{f"{name_field}__startswith": prefix},
    ).values_list(name_field, flat=True)

    pattern = re.compile(rf"{re.escape(prefix)}(\d+)$")
    max_num = 0
    for name in existing:
        if not name:
            continue
        match = pattern.match(name)
        if match:
            max_num = max(max_num, int(match.group(1)))

    return f"{prefix}{max_num + 1:05d}"


def _is_name_conflict(exc, constraint, name_field):
    """True when ``exc`` is our uniqueness constraint firing.

    Backends word this differently, and both forms must be recognised:

    * PostgreSQL names the constraint —
      ``duplicate key value violates unique constraint "uniq_..._org_name"``
    * SQLite names the columns instead —
      ``UNIQUE constraint failed: tally_tallyvendorbill.organization_id,
      tally_tallyvendorbill.bill_munshi_name``

    Any other IntegrityError (a bad FK, a NOT NULL violation) must propagate
    untouched — retrying those would loop and hide a real error.
    """
    message = str(exc).lower()
    if constraint.lower() in message:
        return True
    return "unique" in message and name_field.lower() in message


def save_with_unique_name(
    instance,
    super_save,
    *,
    name_field,
    code,
    constraint,
    should_generate,
    args=(),
    kwargs=None,
):
    """Persist ``instance``, assigning a unique generated name if needed.

    ``super_save`` is the bound ``super().save`` of the calling model, so the
    normal save path is preserved exactly (including ``update_fields``).

    When ``should_generate`` is false — the name is already set, or the model
    isn't ready for one yet — this is a plain pass-through save.
    """
    kwargs = kwargs or {}

    if not should_generate:
        return super_save(*args, **kwargs)

    model = type(instance)
    last_error = None

    for attempt in range(1, MAX_NAME_ATTEMPTS + 1):
        setattr(
            instance,
            name_field,
            next_bill_name(model, instance.organization, code, name_field),
        )
        try:
            # Savepoint: lets us recover from the IntegrityError even when the
            # caller already opened a transaction.
            with transaction.atomic():
                return super_save(*args, **kwargs)
        except IntegrityError as exc:
            if not _is_name_conflict(exc, constraint, name_field):
                raise
            last_error = exc
            logger.info(
                "%s name %s taken (attempt %s/%s) — retrying with the next number.",
                model.__name__,
                getattr(instance, name_field),
                attempt,
                MAX_NAME_ATTEMPTS,
            )
            # Clear so a stale value can never be persisted if we give up.
            setattr(instance, name_field, None)

    logger.error(
        "Gave up generating a unique %s name after %s attempts.",
        model.__name__,
        MAX_NAME_ATTEMPTS,
    )
    raise last_error

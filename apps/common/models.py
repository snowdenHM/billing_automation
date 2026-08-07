import math
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone
import uuid

from apps.common.validators import validate_business_email


class TimeStampedModel(models.Model):
    """Abstract base model with created/updated timestamps."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class SoftDeleteQuerySet(models.QuerySet):
    def alive(self):
        return self.filter(is_deleted=False)

    def deleted(self):
        return self.filter(is_deleted=True)


class SoftDeleteModel(models.Model):
    """Soft delete support (toggle is_deleted instead of hard delete)."""

    is_deleted = models.BooleanField(default=False)

    objects = SoftDeleteQuerySet.as_manager()

    class Meta:
        abstract = True

    def delete(self, using=None, keep_parents=False):  # pragma: no cover
        self.is_deleted = True
        self.save(update_fields=["is_deleted"])


# ---------------------------------------------------------------------------
# Trash — recoverable delete with a fixed retention window
# ---------------------------------------------------------------------------

#: Days a trashed record survives before the purge job destroys it for good.
#: Override with ``TRASH_RETENTION_DAYS`` in settings.
DEFAULT_TRASH_RETENTION_DAYS = 30


def get_trash_retention_days():
    """Retention window in days, read from settings at call time.

    Read lazily rather than captured at import so a deployment can change
    the window without the value being baked into a migration default.
    """
    return int(getattr(settings, "TRASH_RETENTION_DAYS", DEFAULT_TRASH_RETENTION_DAYS))


def trash_cutoff(now=None):
    """The ``deleted_at`` on or before which a trashed row has expired."""
    return (now or timezone.now()) - timedelta(days=get_trash_retention_days())


class TrashableQuerySet(models.QuerySet):
    """Queryset helpers for models carrying :class:`TrashableMixin`."""

    def alive(self):
        """Rows that are not in the trash — what every normal list shows."""
        return self.filter(is_deleted=False)

    def trashed(self):
        """Rows sitting in the trash, expired ones included."""
        return self.filter(is_deleted=True)

    def recoverable(self, now=None):
        """Trashed rows still inside the retention window.

        The Trash UI lists these. Filtering on the cutoff here — rather
        than trusting the purge job to have run — means an expired bill
        stops being offered for restore the moment it expires, even if
        cron is lagging or has been down.
        """
        return self.trashed().filter(deleted_at__gt=trash_cutoff(now))

    def expired(self, now=None):
        """Trashed rows past the retention window, ready to be destroyed.

        ``deleted_at`` is null-checked because a row trashed by an older
        code path would otherwise never satisfy the comparison and would
        linger forever.
        """
        return self.trashed().filter(
            models.Q(deleted_at__lte=trash_cutoff(now)) | models.Q(deleted_at__isnull=True)
        )


class TrashableMixin(models.Model):
    """Recoverable delete: move to trash now, destroy after the window.

    Unlike :class:`SoftDeleteModel` this deliberately does **not** override
    ``delete()``. The purge job and the "delete permanently" action both
    need a real row-and-file destruction, and silently redefining
    ``delete()`` to mean something else is exactly the kind of surprise
    that leaves undeletable rows behind. Trashing is an explicit
    :meth:`move_to_trash` call instead.
    """

    is_deleted = models.BooleanField(
        default=False, db_index=True,
        help_text="True while the record sits in the trash awaiting restore or purge.",
    )
    deleted_at = models.DateTimeField(
        null=True, blank=True, db_index=True,
        help_text="When the record was moved to trash. Drives the retention countdown.",
    )
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="+",
        help_text="User who moved the record to trash.",
    )

    objects = TrashableQuerySet.as_manager()

    class Meta:
        abstract = True

    def move_to_trash(self, user=None):
        """Flag the record as trashed. The uploaded file is left on disk.

        Keeping the file is the whole point — restore has to return a
        working document, so nothing is unlinked until the purge.
        """
        self.is_deleted = True
        self.deleted_at = timezone.now()
        self.deleted_by = user if (user and getattr(user, "is_authenticated", False)) else None
        self.save(update_fields=["is_deleted", "deleted_at", "deleted_by"])

    def restore(self):
        """Bring the record back out of the trash."""
        self.is_deleted = False
        self.deleted_at = None
        self.deleted_by = None
        self.save(update_fields=["is_deleted", "deleted_at", "deleted_by"])

    @property
    def purge_at(self):
        """When this record becomes eligible for permanent deletion."""
        if not self.is_deleted or not self.deleted_at:
            return None
        return self.deleted_at + timedelta(days=get_trash_retention_days())

    @property
    def days_until_purge(self):
        """Whole days left before purge; 0 once expired. None if not trashed."""
        purge_at = self.purge_at
        if purge_at is None:
            return None
        seconds_left = (purge_at - timezone.now()).total_seconds()
        if seconds_left <= 0:
            return 0
        # Round up: with any part of a day left the UI should still say "1 day".
        return math.ceil(seconds_left / 86400)


# ---------------------------------------------------------------------------
# EmailSettings — DB-backed transactional-email config (SendGrid, etc.)
# ---------------------------------------------------------------------------

class EmailSettings(models.Model):
    """Singleton row holding runtime email/SendGrid credentials.

    Storing this in the DB (instead of only ``.env``) lets operations
    rotate the SendGrid key from Django admin without a redeploy — and
    lets us later expose the same fields on an org settings page.

    Load via :func:`apps.common.email_config.get_email_config` which
    caches the value and invalidates on save.
    """

    SINGLETON_PK = 1

    id = models.PositiveIntegerField(primary_key=True, default=SINGLETON_PK, editable=False)
    is_enabled = models.BooleanField(
        default=True,
        help_text="Turn off to force the console-email fallback everywhere.",
    )
    sendgrid_api_key = models.CharField(
        max_length=256,
        blank=True,
        help_text="SendGrid API key (SG.xxxx). Leave blank to use console/SMTP fallback.",
    )
    from_email = models.EmailField(
        default="support@billmunshi.com",
        help_text="Envelope From address. Must be a SendGrid-verified sender.",
    )
    from_name = models.CharField(
        max_length=100,
        default="Bill Munshi",
        help_text="Display name shown in the recipient's inbox.",
    )
    reply_to = models.EmailField(
        default="support@billmunshi.com",
        blank=True,
        help_text="Reply-To address on outbound emails.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Email settings"
        verbose_name_plural = "Email settings"

    def __str__(self):
        return f"Email settings ({'enabled' if self.is_enabled else 'disabled'})"

    def save(self, *args, **kwargs):
        # Enforce a single row — always id=1.
        self.pk = self.SINGLETON_PK
        super().save(*args, **kwargs)
        # Invalidate the module cache so the next send picks up new
        # credentials without a process restart.
        from apps.common.email_config import invalidate_email_config
        invalidate_email_config()

    @classmethod
    def load(cls):
        """Return the singleton, creating it with defaults if absent."""
        obj, _ = cls.objects.get_or_create(pk=cls.SINGLETON_PK)
        return obj

    @property
    def formatted_from(self):
        if self.from_name:
            return f"{self.from_name} <{self.from_email}>"
        return self.from_email


# ---------------------------------------------------------------------------
# DemoRequest — public "Book a demo" lead capture
# ---------------------------------------------------------------------------

class DemoRequest(TimeStampedModel):
    """A demo booking submitted from the public /book-demo page.

    ``email`` is unique so one work address can only ever hold one
    booking — the DB constraint is the real guarantee, the serializer
    check just turns the race into a friendly message.

    Addresses are normalised to lowercase on save so ``Foo@acme.com``
    and ``foo@acme.com`` collide as the same person.
    """

    class Software(models.TextChoices):
        ZOHO = "zoho", "Zoho Books"
        TALLY = "tally", "Tally"
        BOTH = "both", "Both (Zoho Books & Tally)"
        OTHER = "other", "Other / Not sure"

    class Status(models.TextChoices):
        NEW = "new", "New"
        CONTACTED = "contacted", "Contacted"
        SCHEDULED = "scheduled", "Scheduled"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    full_name = models.CharField(max_length=150)
    organization = models.CharField(max_length=150)
    accounting_software = models.CharField(max_length=20, choices=Software.choices)
    email = models.EmailField(
        unique=True,
        validators=[validate_business_email],
        help_text="Work email. Personal mailboxes (Gmail, Yahoo, …) are rejected.",
    )
    phone = models.CharField(max_length=20)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NEW)
    notes = models.TextField(blank=True, help_text="Internal sales notes.")

    class Meta:
        verbose_name = "Demo request"
        verbose_name_plural = "Demo requests"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.full_name} <{self.email}> ({self.organization})"

    def save(self, *args, **kwargs):
        if self.email:
            self.email = self.email.strip().lower()
        super().save(*args, **kwargs)
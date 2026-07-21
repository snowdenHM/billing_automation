from django.db import models
from django.utils import timezone
import uuid


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
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

    # Which service actually carries the mail.
    #
    # This row only ever held a SendGrid key, so SendGrid was the only
    # provider reachable without a code change. When the account hit
    # "Maximum credits exceeded" every signup silently lost its
    # verification email and there was no way to switch without a
    # redeploy. django-anymail has also dropped official SendGrid
    # support, so SMTP is the better default for new installs — it works
    # with Brevo, Mailgun, Postmark, SES, Gmail and anything else.
    PROVIDER_SENDGRID = "sendgrid"
    PROVIDER_SMTP = "smtp"
    PROVIDER_CONSOLE = "console"
    PROVIDER_CHOICES = [
        (PROVIDER_SMTP, "SMTP (Brevo, Mailgun, SES, Gmail, …)"),
        (PROVIDER_SENDGRID, "SendGrid API"),
        (PROVIDER_CONSOLE, "Console (write to logs, send nothing)"),
    ]

    id = models.PositiveIntegerField(primary_key=True, default=SINGLETON_PK, editable=False)
    is_enabled = models.BooleanField(
        default=True,
        help_text="Turn off to force the console-email fallback everywhere.",
    )
    provider = models.CharField(
        max_length=20,
        choices=PROVIDER_CHOICES,
        default=PROVIDER_SENDGRID,
        help_text=(
            "Which service sends the mail. Existing installs default to "
            "SendGrid so nothing changes on upgrade; switch to SMTP to use "
            "any other provider without a redeploy."
        ),
    )
    sendgrid_api_key = models.CharField(
        max_length=256,
        blank=True,
        help_text="SendGrid API key (SG.xxxx). Only used when Provider is SendGrid.",
    )

    # --- SMTP credentials (used when Provider is SMTP) ---
    smtp_host = models.CharField(
        max_length=255, blank=True,
        help_text="e.g. smtp-relay.brevo.com, smtp.mailgun.org, email-smtp.ap-south-1.amazonaws.com",
    )
    smtp_port = models.PositiveIntegerField(
        default=587,
        help_text="587 for STARTTLS (usual), 465 for implicit SSL.",
    )
    smtp_username = models.CharField(max_length=255, blank=True)
    smtp_password = models.CharField(max_length=512, blank=True)
    smtp_use_tls = models.BooleanField(
        default=True, help_text="STARTTLS. Use with port 587.",
    )
    smtp_use_ssl = models.BooleanField(
        default=False,
        help_text="Implicit SSL. Use with port 465. Do not enable together with STARTTLS.",
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

    def clean(self):
        """Catch provider mistakes in admin rather than at send time.

        A bad combination here is invisible until a user signs up and no
        mail arrives, so it is worth refusing the save.
        """
        from django.core.exceptions import ValidationError

        errors = {}

        if self.is_enabled and self.provider == self.PROVIDER_SMTP:
            if not self.smtp_host:
                errors["smtp_host"] = "Required when the provider is SMTP."
            if self.smtp_use_tls and self.smtp_use_ssl:
                msg = (
                    "Pick one. STARTTLS goes with port 587, implicit SSL with "
                    "port 465."
                )
                errors["smtp_use_tls"] = msg
                errors["smtp_use_ssl"] = msg
            elif not self.smtp_use_tls and not self.smtp_use_ssl:
                errors["smtp_use_tls"] = (
                    "Enable STARTTLS (port 587) or implicit SSL (port 465) — "
                    "sending credentials over an unencrypted connection is "
                    "not allowed."
                )

        if (
            self.is_enabled
            and self.provider == self.PROVIDER_SENDGRID
            and not self.sendgrid_api_key
        ):
            errors["sendgrid_api_key"] = "Required when the provider is SendGrid."

        if errors:
            raise ValidationError(errors)

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
    # Client Correction 57: the requester confirms the work email through a
    # link we send them; sales can filter on this in admin.
    email_verified = models.BooleanField(default=False)
    email_verified_at = models.DateTimeField(null=True, blank=True)

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

class BillActivity(models.Model):
    """Append-only record of everything that happens to a bill.

    One store for every module and every bill type. The bill is addressed
    by ``(module, bill_type, bill_id)`` rather than a ForeignKey: the six
    bill models live in two apps, all use UUID primary keys, and the log
    must outlive the row it describes -- a deletion is itself an event
    worth keeping.

    It also carries the raw material for suggestion learning. When a user
    overrides something the system proposed, the change entry keeps the
    proposal alongside the chosen value, so "for this vendor, what does
    this organisation actually pick" is answerable from here without a
    second capture mechanism.
    """

    class Module(models.TextChoices):
        TALLY = "tally", "Tally"
        ZOHO = "zoho", "Zoho"

    class BillType(models.TextChoices):
        VENDOR = "vendor", "Purchase Voucher"
        EXPENSE = "expense", "Journal Entry"
        PAYMENT = "payment", "Payment Voucher"
        JOURNAL = "journal", "Journal"
        SALES = "sales", "Sales Voucher"
        RECEIPT = "receipt", "Receipt Voucher"

    class Action(models.TextChoices):
        UPLOADED = "uploaded", "Uploaded"
        ANALYSIS_QUEUED = "analysis_queued", "Analysis queued"
        ANALYSIS_COMPLETED = "analysis_completed", "Analysis completed"
        ANALYSIS_FAILED = "analysis_failed", "Analysis failed"
        DUPLICATE_FLAGGED = "duplicate_flagged", "Flagged as duplicate"
        EXTRACTED = "extracted", "Extracted"
        # No standalone "edited" action: every field change today goes
        # through verify, and is carried on the VERIFIED event's change
        # set. Add one when a save-without-verify endpoint exists.
        VERIFIED = "verified", "Verified"
        APPROVAL_SUBMITTED = "approval_submitted", "Submitted for approval"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Sent back"
        SYNC_REQUESTED = "sync_requested", "Sent to accounting software"
        SYNC_CONFIRMED = "sync_confirmed", "Posted"
        SYNC_FAILED = "sync_failed", "Posting failed"
        MOVED = "moved", "Moved"
        TRASHED = "trashed", "Moved to Trash"
        RESTORED = "restored", "Restored from Trash"
        DELETED = "deleted", "Deleted permanently"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE,
        related_name="bill_activities",
    )
    module = models.CharField(max_length=16, choices=Module.choices)
    bill_type = models.CharField(max_length=16, choices=BillType.choices)
    bill_id = models.UUIDField()

    action = models.CharField(max_length=32, choices=Action.choices)
    # Null for anything a background job did. ``actor_label`` keeps a
    # readable name even after the user row is gone.
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="bill_activities",
    )
    actor_label = models.CharField(max_length=255, blank=True, default="")

    summary = models.CharField(max_length=255, blank=True, default="")
    # [{"field": "total", "from": "100.00", "to": "120.00",
    #   "suggested": "100.00"}] -- ``suggested`` only when the system
    # proposed something and the user chose otherwise.
    changes = models.JSONField(default=list, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Bill Activity"
        verbose_name_plural = "Bill Activity"
        ordering = ("-created_at", "-id")
        indexes = [
            # The per-bill timeline.
            models.Index(fields=["bill_id", "-created_at"],
                         name="billactivity_bill_idx"),
            # The organisation-wide feed, and its filters.
            models.Index(fields=["organization", "-created_at"],
                         name="billactivity_org_idx"),
            models.Index(fields=["organization", "action", "-created_at"],
                         name="billactivity_org_action_idx"),
            # Suggestion learning reads by (org, module, bill_type).
            models.Index(fields=["organization", "module", "bill_type", "-created_at"],
                         name="billactivity_learn_idx"),
        ]

    def __str__(self):
        return f"{self.get_action_display()} on {self.bill_type}:{self.bill_id}"


class BillApproval(models.Model):
    """A bill waiting for, or carrying, a manager's decision.

    Kept beside the bill rather than on it. ``status`` on the three bill
    models is a four-value field that the sync filters, the frontend
    tabs and the Tally payload all read; adding a fifth value to it
    would change behaviour for every organisation at once, including the
    ones using the product today. An approval lives here instead, so an
    organisation that has not switched the workflow on is unaffected by
    its existence.

    Addressed by ``(module, bill_type, bill_id)`` for the same reasons
    as :class:`BillActivity`: six bill models, two apps, UUID keys.
    """

    class State(models.TextChoices):
        PENDING = "pending", "Waiting for approval"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Sent back"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE,
        related_name="bill_approvals",
    )
    module = models.CharField(max_length=16, choices=BillActivity.Module.choices)
    bill_type = models.CharField(max_length=16, choices=BillActivity.BillType.choices)
    bill_id = models.UUIDField()

    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING)

    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="bill_approvals_submitted",
    )
    submitted_at = models.DateTimeField(default=timezone.now)
    submitted_note = models.CharField(max_length=500, blank=True, default="")

    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="bill_approvals_decided",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_note = models.CharField(max_length=500, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Bill Approval"
        verbose_name_plural = "Bill Approvals"
        ordering = ("-submitted_at", "-id")
        constraints = [
            # One live decision per bill. Re-submitting a rejected bill
            # reuses the row rather than stacking a second one, so "is
            # this bill approved" is a single lookup with no ordering
            # question.
            models.UniqueConstraint(
                fields=["module", "bill_type", "bill_id"],
                name="billapproval_one_per_bill",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "state", "-submitted_at"],
                         name="billapproval_queue_idx"),
            models.Index(fields=["bill_id"], name="billapproval_bill_idx"),
        ]

    def __str__(self):
        return f"{self.get_state_display()}: {self.bill_type}:{self.bill_id}"


class ExtractionSettings(models.Model):
    """Singleton row holding the bill-reading route configuration.

    These were environment variables, which meant a redeploy to try a
    different route and no way to roll one back in a hurry. They are
    the kind of setting somebody wants to flip while watching what
    happens, so they belong somewhere a person can reach.

    Every field is **nullable or blank on purpose**: empty means "use
    whatever the environment says", so this row only ever overrides what
    an operator has deliberately set. A fresh install behaves exactly as
    it did before the row existed, and clearing a field hands control
    back to the deployment rather than to a hardcoded default.

    Read through :func:`apps.common.extraction_config.get_extraction_config`,
    which caches it and falls back to settings if the table is missing.
    """

    SINGLETON_PK = 1

    DETAIL_CHOICES = [
        ("high", "High — full resolution, ~1,200 tokens a page"),
        ("low", "Low — flat 85 tokens a page; only safe with OCR assist"),
    ]

    # --- which route reads a bill -----------------------------------
    ocr_first = models.BooleanField(
        null=True, blank=True, verbose_name="OCR-first route",
        help_text=(
            "Read the bill with on-device OCR and send the TEXT to the cheap "
            "model, falling back to the vision model when OCR is poor. "
            "Measured on this client's scans: 10% of bills matched the "
            "current route on every field. Empty = follow EXTRACTION_OCR_FIRST."
        ),
    )
    ocr_assist = models.BooleanField(
        null=True, blank=True, verbose_name="OCR assist",
        help_text=(
            "Send the OCR text ALONGSIDE the page image rather than instead "
            "of it, which allows a low-detail image. Cheaper, but measured "
            "at 24% full-field agreement on this client's scans — the model "
            "anchors on OCR's misread digits. Empty = follow "
            "EXTRACTION_OCR_ASSIST."
        ),
    )
    ocr_shadow = models.BooleanField(
        null=True, blank=True, verbose_name="Shadow mode",
        help_text=(
            "Run the OCR route alongside the live one and record what it "
            "would have produced, while still returning the live result. "
            "Costs both calls. Empty = follow EXTRACTION_OCR_SHADOW."
        ),
    )
    rollout_organizations = models.ManyToManyField(
        "organizations.Organization", blank=True,
        related_name="extraction_rollout",
        verbose_name="OCR-first for these organisations only",
        help_text=(
            "Organisations on the OCR-first route even while the global "
            "switch above is off. Lets a route be proven on one client "
            "before it reaches the rest."
        ),
    )

    # --- models and detail -------------------------------------------
    vision_model = models.CharField(
        max_length=64, blank=True, default="",
        help_text="Empty = follow EXTRACTION_VISION_MODEL (gpt-4o).",
    )
    text_model = models.CharField(
        max_length=64, blank=True, default="",
        help_text="Empty = follow EXTRACTION_TEXT_MODEL (gpt-4o-mini).",
    )
    vision_detail = models.CharField(
        max_length=8, blank=True, default="", choices=DETAIL_CHOICES,
        help_text=(
            "Image detail on the vision call. Only drop to low with OCR "
            "assist on — on its own it trades accuracy for fractions of a "
            "cent. Empty = follow EXTRACTION_VISION_DETAIL."
        ),
    )

    # --- when OCR text is considered good enough ----------------------
    ocr_min_confidence = models.FloatField(
        null=True, blank=True,
        help_text=(
            "Below this mean OCR confidence the page goes to the vision "
            "model. Note: measured here, confidence did NOT predict whether "
            "the answer was right. Empty = follow "
            "EXTRACTION_OCR_MIN_CONFIDENCE (0.90)."
        ),
    )
    ocr_min_chars = models.IntegerField(
        null=True, blank=True,
        help_text=(
            "A near-empty page read confidently is the handwriting case: "
            "send the image. Empty = follow EXTRACTION_OCR_MIN_CHARS."
        ),
    )

    # --- upload handling ----------------------------------------------
    split_batches = models.BooleanField(
        null=True, blank=True, verbose_name="Split batch scans",
        help_text=(
            "Split an upload that OCR says is several bills in one file. "
            "Empty = follow EXTRACTION_SPLIT_BATCHES."
        ),
    )

    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )

    class Meta:
        verbose_name = "Extraction Settings"
        verbose_name_plural = "Extraction Settings"

    def __str__(self):
        return "Extraction Settings"

    def save(self, *args, **kwargs):
        self.pk = self.SINGLETON_PK
        super().save(*args, **kwargs)
        from apps.common.extraction_config import invalidate_extraction_config
        invalidate_extraction_config()

    def delete(self, *args, **kwargs):
        """Refuse to delete: the row is the configuration, not a record."""
        return

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=cls.SINGLETON_PK)
        return obj

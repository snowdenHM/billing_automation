import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class SupportTicket(models.Model):
    """A support ticket raised by a signed-in user (or, for embeds/anon
    entry points, by nobody at all — organization/user are nullable so the
    widget can be reused from surfaces that don't require auth)."""

    CATEGORY_BUG = "bug"
    CATEGORY_FEATURE = "feature"
    CATEGORY_QUESTION = "question"
    CATEGORY_BILLING = "billing"
    CATEGORY_OTHER = "other"
    CATEGORY_CHOICES = [
        (CATEGORY_BUG, "Bug"),
        (CATEGORY_FEATURE, "Feature Request"),
        (CATEGORY_QUESTION, "Question"),
        (CATEGORY_BILLING, "Billing"),
        (CATEGORY_OTHER, "Other"),
    ]

    STATUS_OPEN = "open"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_RESOLVED = "resolved"
    STATUS_CLOSED = "closed"
    STATUS_CHOICES = [
        (STATUS_OPEN, "Open"),
        (STATUS_IN_PROGRESS, "In Progress"),
        (STATUS_RESOLVED, "Resolved"),
        (STATUS_CLOSED, "Closed"),
    ]

    PRIORITY_LOW = "low"
    PRIORITY_MEDIUM = "medium"
    PRIORITY_HIGH = "high"
    PRIORITY_CHOICES = [
        (PRIORITY_LOW, "Low"),
        (PRIORITY_MEDIUM, "Medium"),
        (PRIORITY_HIGH, "High"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="support_tickets",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="support_tickets",
    )
    subject = models.CharField(max_length=200)
    message = models.TextField()
    category = models.CharField(
        max_length=20, choices=CATEGORY_CHOICES, default=CATEGORY_OTHER
    )
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_OPEN
    )
    priority = models.CharField(
        max_length=10, choices=PRIORITY_CHOICES, default=PRIORITY_MEDIUM
    )
    page_url = models.URLField(max_length=500, blank=True, default="")
    browser = models.CharField(max_length=200, blank=True, default="")

    # Trashable-lite: soft delete flag, kept optional/simple rather than
    # pulling in apps.common's full Trashable mixin (which carries its own
    # migration dependencies) — a plain boolean is enough for admin cleanup.
    is_deleted = models.BooleanField(default=False)

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"[{self.get_status_display()}] {self.subject}"

    def mark_resolved(self):
        self.status = self.STATUS_RESOLVED
        self.resolved_at = timezone.now()
        self.save(update_fields=["status", "resolved_at", "updated_at"])


class SupportTicketMessage(models.Model):
    """A single message in a ticket's conversation thread — either the
    submitter following up, or an admin replying (or leaving an internal
    note only other staff can see)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ticket = models.ForeignKey(
        SupportTicket, on_delete=models.CASCADE, related_name="messages"
    )
    author_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="support_ticket_messages",
    )
    # Populated when an admin replies on behalf of an address that isn't
    # (or isn't only) a User — keeps the thread readable even if author_user
    # is null (e.g. a deleted account, or a reply sent from the mailbox).
    author_email = models.EmailField(blank=True, default="")
    body = models.TextField()
    is_internal = models.BooleanField(
        default=False,
        help_text="Internal note — visible to admins only, never emailed to the submitter.",
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"Message on {self.ticket_id} @ {self.created_at:%Y-%m-%d %H:%M}"


class SupportTicketRecipient(models.Model):
    """The dev-team distribution list for support notifications."""

    email = models.EmailField(unique=True)
    name = models.CharField(max_length=100, blank=True, default="")
    receive_new = models.BooleanField(
        default=True, help_text="Notify this address when a new ticket is created."
    )
    receive_reply = models.BooleanField(
        default=False,
        help_text="Notify this address when a submitter replies to a ticket.",
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ["email"]

    def __str__(self):
        return f"{self.name} <{self.email}>" if self.name else self.email

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

    # Client Correction 51: the floating support chat (app + public website)
    # asks for name / email / message. The sender may not be logged in, so
    # their details are kept on the ticket itself.
    SOURCE_TICKET = "ticket"
    SOURCE_CHAT = "chat"
    SOURCE_CHOICES = [
        (SOURCE_TICKET, "Support ticket"),
        (SOURCE_CHAT, "Support chat"),
    ]
    source = models.CharField(
        max_length=10, choices=SOURCE_CHOICES, default=SOURCE_TICKET
    )
    contact_name = models.CharField(max_length=120, blank=True, default="")
    contact_email = models.EmailField(blank=True, default="")

    # Trashable-lite: soft delete flag, kept optional/simple rather than
    # pulling in apps.common's full Trashable mixin (which carries its own
    # migration dependencies) — a plain boolean is enough for admin cleanup.
    is_deleted = models.BooleanField(default=False)

    # Desk ownership. Set by auto-assignment on creation and freely
    # reassigned afterwards — advisory, not a lock, so two agents are never
    # stuck waiting on whoever the round robin happened to pick.
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_support_tickets",
    )
    assigned_at = models.DateTimeField(null=True, blank=True)

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


# ---------------------------------------------------------------------------
# Consent-gated account access
# ---------------------------------------------------------------------------
# The shortcut this replaces is a "log in as user" button for support staff.
# That makes access a standing capability: it exists before the client asks,
# outlives the reason for it, and the client can neither see nor stop it.
# Every model below exists to take one of those properties away — the grant
# is minted by the client, bound to one ticket, time-boxed, revocable, and
# every request made through it is written down against the agent.


class SupportAgent(models.Model):
    """A member of the support team.

    Deliberately a row of its own rather than a read of ``is_staff``.
    Several admin flows flip ``is_staff`` (it is what the Django admin
    gate reads), and any of them would otherwise mint a support agent who
    can see into client accounts. Being an agent requires both: the staff
    flag *and* an active row here.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="support_agent",
    )
    # Clients see this on the access banner. Showing the raw email would
    # publish a staff address to every account the agent ever opens.
    display_name = models.CharField(max_length=120)
    # Off means no *new* assignments. Existing tickets keep working, because
    # deactivating an agent mid-shift must not orphan their open queue.
    is_active = models.BooleanField(default=True)
    max_open_tickets = models.PositiveIntegerField(default=15)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ["display_name"]

    def __str__(self):
        return self.display_name


class SupportAccessGrant(models.Model):
    """One client's "yes" to one agent looking at one account, for a while.

    The plaintext code is returned exactly once, at mint, and never stored.
    A database reader holds only ``sha256(code)``, so the trail cannot be
    replayed out of a backup.
    """

    SCOPE_READ_ONLY = "read_only"
    SCOPE_FULL = "full"
    SCOPE_CHOICES = [
        (SCOPE_READ_ONLY, "Read only"),
        (SCOPE_FULL, "Full"),
    ]

    STATUS_PENDING = "pending"
    STATUS_ACTIVE = "active"
    STATUS_EXPIRED = "expired"
    STATUS_REVOKED = "revoked"
    STATUS_CLOSED = "closed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_ACTIVE, "Active"),
        (STATUS_EXPIRED, "Expired"),
        (STATUS_REVOKED, "Revoked"),
        (STATUS_CLOSED, "Closed"),
    ]

    # Default TTL, and the ceiling a client admin may ask for. The cap is
    # here and not in the serializer alone so a second caller (a management
    # command, a future endpoint) cannot mint an eight-day grant.
    DEFAULT_TTL_MINUTES = 60
    MAX_TTL_MINUTES = 8 * 60

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Required: a grant always carries the reason it exists. Without the
    # ticket there is nothing for the closing of the ticket to revoke.
    ticket = models.ForeignKey(
        SupportTicket, on_delete=models.CASCADE, related_name="access_grants"
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="support_access_grants",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="minted_support_grants",
    )
    # First group of the code only ("BM-7KQ4"). Lets both sides say "the
    # one ending 7KQ4" on the phone without reading out the secret.
    code_label = models.CharField(max_length=9)
    token_hash = models.CharField(max_length=64, unique=True)
    scope = models.CharField(max_length=20, choices=SCOPE_CHOICES, default=SCOPE_READ_ONLY)
    expires_at = models.DateTimeField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)

    redeemed_at = models.DateTimeField(null=True, blank=True)
    redeemed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="redeemed_support_grants",
    )

    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="revoked_support_grants",
    )
    revoke_reason = models.CharField(max_length=200, blank=True, default="")

    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["organization", "status"]),
            models.Index(fields=["token_hash"]),
        ]

    def __str__(self):
        return f"{self.code_label} ({self.status})"

    @property
    def is_expired(self):
        return timezone.now() >= self.expires_at

    def is_usable(self, *, now=None):
        """True when this grant may carry a request *right now*.

        Expiry is computed from the clock, not read from ``status``. The
        status column is tidied by a sweeper, and a grant whose TTL has
        passed must stop working the moment it passes — not the moment a
        cron gets round to relabelling it.
        """
        now = now or timezone.now()
        if self.status in (self.STATUS_REVOKED, self.STATUS_CLOSED, self.STATUS_EXPIRED):
            return False
        return now < self.expires_at

    def revoke(self, *, by=None, reason="", end_reason=None):
        """Kill the grant and every live session on it, now."""
        if self.status in (self.STATUS_REVOKED, self.STATUS_CLOSED):
            return False
        now = timezone.now()
        self.status = self.STATUS_REVOKED
        self.revoked_at = now
        self.revoked_by = by
        self.revoke_reason = reason[:200]
        self.save(update_fields=["status", "revoked_at", "revoked_by", "revoke_reason"])
        self.sessions.filter(ended_at__isnull=True).update(
            ended_at=now,
            end_reason=end_reason or SupportAccessSession.END_REVOKED,
        )
        return True


class SupportAccessSession(models.Model):
    """One agent's use of one grant. Ends when they sign out, the TTL
    passes, the client revokes, or the ticket is shut."""

    END_SIGNED_OUT = "signed_out"
    END_EXPIRED = "expired"
    END_REVOKED = "revoked"
    END_TICKET_CLOSED = "ticket_closed"
    END_REASON_CHOICES = [
        (END_SIGNED_OUT, "Signed out"),
        (END_EXPIRED, "Expired"),
        (END_REVOKED, "Revoked"),
        (END_TICKET_CLOSED, "Ticket closed"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    grant = models.ForeignKey(
        SupportAccessGrant, on_delete=models.CASCADE, related_name="sessions"
    )
    agent = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="support_access_sessions",
    )
    started_at = models.DateTimeField(default=timezone.now, editable=False)
    ended_at = models.DateTimeField(null=True, blank=True)
    ip = models.CharField(max_length=45, blank=True, default="")
    user_agent = models.CharField(max_length=300, blank=True, default="")
    end_reason = models.CharField(
        max_length=20, choices=END_REASON_CHOICES, blank=True, default=""
    )

    class Meta:
        ordering = ["-started_at"]
        indexes = [models.Index(fields=["agent", "ended_at"])]

    def __str__(self):
        return f"session {self.id} on {self.grant_id}"

    @property
    def is_live(self):
        return self.ended_at is None

    def end(self, reason=END_SIGNED_OUT):
        if self.ended_at is not None:
            return False
        self.ended_at = timezone.now()
        self.end_reason = reason
        self.save(update_fields=["ended_at", "end_reason"])
        return True


class SupportAccessEvent(models.Model):
    """One row per request made while impersonating.

    Bodies are not kept. They are the client's invoice data, and copying
    them into an audit table would mean the trail leaks exactly what the
    grant was meant to fence. The trail records what was touched, not what
    it said.
    """

    session = models.ForeignKey(
        SupportAccessSession, on_delete=models.CASCADE, related_name="events"
    )
    method = models.CharField(max_length=10)
    path = models.CharField(max_length=500)
    status_code = models.PositiveSmallIntegerField()
    at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ["-at"]
        indexes = [models.Index(fields=["session", "at"])]

    def __str__(self):
        return f"{self.method} {self.path} -> {self.status_code}"

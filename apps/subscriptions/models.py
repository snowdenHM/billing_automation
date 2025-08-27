from django.db import models
from django.conf import settings
from django.utils import timezone
from django.core.exceptions import ValidationError
from datetime import timedelta
import uuid

from apps.common.models import TimeStampedModel
from apps.organizations.models import Organization, OrgMembership


class Plan(TimeStampedModel):
    """
    Subscription plan that defines pricing, user limits, and features.
    """
    MONTHLY = "MONTHLY"
    YEARLY = "YEARLY"
    CYCLE_CHOICES = [(MONTHLY, "Monthly"), (YEARLY, "Yearly")]

    code = models.SlugField(max_length=50, unique=True)
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    max_users = models.PositiveIntegerField(default=5)
    features = models.JSONField(default=dict, blank=True)
    billing_cycle = models.CharField(max_length=10, choices=CYCLE_CHOICES, default=MONTHLY)
    price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    is_active = models.BooleanField(default=True, help_text="Whether this plan is available for subscription")
    trial_days = models.PositiveIntegerField(default=0, help_text="Number of trial days. 0 means no trial.")

    # Module entitlements
    modules = models.JSONField(default=list, blank=True, help_text="List of module codes this plan provides access to")

    class Meta:
        ordering = ("price", "code")

    def __str__(self):
        return f"{self.name} ({self.code})"

    def calculate_end_date(self, start_date=None):
        """
        Calculate the end date based on billing cycle.
        """
        if start_date is None:
            start_date = timezone.now()

        if self.billing_cycle == self.MONTHLY:
            return start_date + timedelta(days=30)
        elif self.billing_cycle == self.YEARLY:
            return start_date + timedelta(days=365)
        return start_date


class Subscription(TimeStampedModel):
    """
    Organization subscription to a specific plan with status tracking.
    """
    ACTIVE = "ACTIVE"
    TRIAL = "TRIAL"
    EXPIRED = "EXPIRED"
    CANCELED = "CANCELED"
    STATUS_CHOICES = [
        (ACTIVE, "Active"),
        (TRIAL, "Trial"),
        (EXPIRED, "Expired"),
        (CANCELED, "Canceled")
    ]

    organization = models.OneToOneField(
        Organization,
        on_delete=models.CASCADE,
        related_name="subscription"
    )
    plan = models.ForeignKey(
        Plan,
        on_delete=models.PROTECT,
        related_name="subscriptions"
    )
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=TRIAL)
    starts_at = models.DateTimeField(default=timezone.now)
    ends_at = models.DateTimeField(null=True, blank=True)
    canceled_at = models.DateTimeField(null=True, blank=True)
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="assigned_org_subscriptions"
    )

    # Metadata
    notes = models.TextField(blank=True)
    auto_renew = models.BooleanField(default=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.organization.name} → {self.plan.name} ({self.get_status_display()})"

    def clean(self):
        """Validate subscription constraints"""
        # Validate max users constraint
        user_count = OrgMembership.objects.filter(organization=self.organization, is_active=True).count()
        if self.plan and user_count > self.plan.max_users:
            raise ValidationError({
                "plan": f"Current active members ({user_count}) exceed plan limit ({self.plan.max_users})."
            })

    def save(self, *args, **kwargs):
        """Set appropriate defaults on save"""
        # Set end date based on plan if not provided
        if not self.ends_at and self.plan:
            self.ends_at = self.plan.calculate_end_date(self.starts_at)

        # Set initial status based on trial period
        if not self.id:  # New subscription
            if self.plan.trial_days > 0:
                self.status = self.TRIAL
                self.ends_at = self.starts_at + timedelta(days=self.plan.trial_days)
            else:
                self.status = self.ACTIVE

        super().save(*args, **kwargs)

    def is_valid(self):
        """Check if subscription is valid (active or in trial)"""
        if self.status in [self.EXPIRED, self.CANCELED]:
            return False

        now = timezone.now()
        if self.ends_at and now > self.ends_at:
            # Auto-update status if expired
            if self.status != self.EXPIRED:
                self.status = self.EXPIRED
                self.save(update_fields=["status"])
            return False

        return True

    def cancel(self, save=True):
        """Cancel subscription but maintain access until end date"""
        self.status = self.CANCELED
        self.canceled_at = timezone.now()
        self.auto_renew = False
        if save:
            self.save(update_fields=["status", "canceled_at", "auto_renew"])

    def renew(self, days=None):
        """Renew subscription for another period"""
        now = timezone.now()
        start_date = max(now, self.ends_at or now)

        if days:
            self.ends_at = start_date + timedelta(days=days)
        else:
            self.ends_at = self.plan.calculate_end_date(start_date)

        self.status = self.ACTIVE
        self.save(update_fields=["status", "ends_at"])

    def change_plan(self, new_plan):
        """Change subscription to a new plan"""
        self.plan = new_plan
        self.status = self.ACTIVE
        self.ends_at = new_plan.calculate_end_date(timezone.now())
        self.save()

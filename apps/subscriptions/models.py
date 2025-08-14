from django.db import models
from django.conf import settings
from django.utils import timezone
from apps.common.models import TimeStampedModel
from apps.organizations.models import Organization, OrgMembership


class Plan(TimeStampedModel):
    MONTHLY = "MONTHLY"
    YEARLY = "YEARLY"
    CYCLE_CHOICES = [(MONTHLY, "Monthly"), (YEARLY, "Yearly")]

    code = models.SlugField(max_length=50, unique=True)
    name = models.CharField(max_length=100)
    max_users = models.PositiveIntegerField(default=5)
    features = models.JSONField(default=dict, blank=True)
    billing_cycle = models.CharField(max_length=10, choices=CYCLE_CHOICES, default=MONTHLY)
    price = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    class Meta:
        ordering = ("code",)

    def __str__(self):  # pragma: no cover
        return f"{self.name} ({self.code})"


class Subscription(TimeStampedModel):
    ACTIVE = "ACTIVE"
    TRIAL = "TRIAL"
    EXPIRED = "EXPIRED"
    STATUS_CHOICES = [(ACTIVE, "Active"), (TRIAL, "Trial"), (EXPIRED, "Expired")]

    organization = models.OneToOneField(Organization, on_delete=models.CASCADE, related_name="subscription")
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="subscriptions")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=ACTIVE)
    starts_at = models.DateTimeField(default=timezone.now)
    ends_at = models.DateTimeField(null=True, blank=True)
    assigned_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="assigned_org_subscriptions")

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):  # pragma: no cover
        return f"{self.organization.name} -> {self.plan.name}"

    def clean(self):
        # Validate max users constraint
        from django.core.exceptions import ValidationError
        user_count = OrgMembership.objects.filter(organization=self.organization, is_active=True).count()
        if self.plan and user_count > self.plan.max_users:
            raise ValidationError({
                "plan": f"Current active members ({user_count}) exceed plan limit ({self.plan.max_users})."
            })
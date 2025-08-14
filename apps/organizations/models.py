from django.conf import settings
from django.db import models
from django.utils.text import slugify
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from apps.common.models import TimeStampedModel
from rest_framework_api_key.models import APIKey
from waffle.models import Switch


class Organization(TimeStampedModel):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    STATUS_CHOICES = [(ACTIVE, "Active"), (SUSPENDED, "Suspended")]

    name = models.CharField(max_length=255, unique=True)
    slug = models.SlugField(max_length=255, unique=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=ACTIVE)

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="owned_organizations",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_organizations",
    )

    # M2M to modules, through explicit entitlement table
    # (Gives room for metadata later like start/end dates)
    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        return super().save(*args, **kwargs)

    def __str__(self):  # pragma: no cover
        return self.name


class OrgMembership(TimeStampedModel):
    ADMIN = "ADMIN"
    MEMBER = "MEMBER"
    ROLE_CHOICES = [(ADMIN, "Admin"), (MEMBER, "Member")]

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default=MEMBER)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ("organization", "user")
        ordering = ("organization_id", "-id")

    def __str__(self):  # pragma: no cover
        return f"{self.user_id} in {self.organization_id} ({self.role})"


class OrganizationAPIKey(TimeStampedModel):
    api_key = models.OneToOneField(APIKey, on_delete=models.CASCADE, related_name="organization_link")
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="api_keys")
    name = models.CharField(max_length=100)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="issued_org_api_keys")

    class Meta:
        unique_together = ("api_key", "organization")
        ordering = ("-id",)

    def __str__(self):  # pragma: no cover
        return f"{self.organization.name} · {self.name}"


# ---------- Modules & Entitlements ----------

class Module(TimeStampedModel):
    """
    Catalog of sellable/enable-able modules (e.g., 'zoho', 'tally').
    """
    code = models.SlugField(max_length=50, unique=True)
    name = models.CharField(max_length=100)

    class Meta:
        ordering = ("code",)

    def __str__(self):  # pragma: no cover
        return f"{self.name} ({self.code})"


class OrganizationModule(TimeStampedModel):
    """
    Entitlement table: which organization has which module.
    """
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="org_modules")
    module = models.ForeignKey(Module, on_delete=models.CASCADE, related_name="org_modules")
    is_enabled = models.BooleanField(default=True)

    class Meta:
        unique_together = ("organization", "module")
        ordering = ("organization_id", "module_id")

    def __str__(self):  # pragma: no cover
        return f"{self.organization_id}:{self.module.code} -> {self.is_enabled}"


def _switch_name(org_id: int, module_code: str) -> str:
    return f"org:{org_id}:{module_code}"


@receiver(post_save, sender=OrganizationModule)
def sync_switch_on_save(sender, instance: "OrganizationModule", **kwargs):
    """
    Keep waffle Switch in sync with entitlement.
    """
    sw, _ = Switch.objects.get_or_create(name=_switch_name(instance.organization_id, instance.module.code))
    if sw.active != instance.is_enabled:
        sw.active = instance.is_enabled
        sw.save(update_fields=["active"])


@receiver(post_delete, sender=OrganizationModule)
def sync_switch_on_delete(sender, instance: "OrganizationModule", **kwargs):
    """
    Disable switch when entitlement removed.
    """
    try:
        sw = Switch.objects.get(name=_switch_name(instance.organization_id, instance.module.code))
        if sw.active:
            sw.active = False
            sw.save(update_fields=["active"])
    except Switch.DoesNotExist:
        pass

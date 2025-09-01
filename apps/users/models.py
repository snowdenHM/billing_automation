from django.contrib.auth.models import AbstractUser
from django.contrib.auth.hashers import make_password
from django.db import models
from django.utils.translation import gettext_lazy as _
from django.utils import timezone

from .managers import UserManager


class User(AbstractUser):
    """Custom user model with email as username.

    We keep the username field but make it optional; the primary login field is email.
    Additional fields help track user status and provide more user information.
    """

    username = models.CharField(max_length=150, unique=False, blank=True, null=True)
    email = models.EmailField(_("email address"), unique=True)
    phone_number = models.CharField(max_length=15, blank=True, null=True)

    # Profile information
    bio = models.TextField(blank=True)
    profile_image = models.URLField(blank=True, null=True)

    # Account status
    email_verified = models.BooleanField(default=False)
    last_active = models.DateTimeField(null=True, blank=True)

    # Settings and preferences
    notification_preferences = models.JSONField(default=dict, blank=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []  # no username required

    objects = UserManager()

    class Meta:
        verbose_name = _("user")
        verbose_name_plural = _("users")
        ordering = ["-date_joined"]

    def __str__(self) -> str:
        return self.email

    def get_full_name(self) -> str:
        """
        Return the first_name plus the last_name, with a space in between.
        """
        full_name = f"{self.first_name} {self.last_name}"
        return full_name.strip()

    def get_short_name(self) -> str:
        """Return the first name."""
        return self.first_name

    def update_last_active(self):
        """Update the last active timestamp."""
        self.last_active = timezone.now()
        self.save(update_fields=["last_active"])

    def send_verification_email(self):
        """Send an email verification to the user."""
        # Implementation would depend on your email sending setup
        pass

    def save(self, *args, **kwargs):
        # Handle password hashing when saving
        if self._password is not None:
            self.password = make_password(self._password)
            self._password = None
        super().save(*args, **kwargs)

    def set_password(self, raw_password):
        """Override to ensure proper password hashing."""
        if raw_password is not None:
            self.password = make_password(raw_password)
            self._password = None
            # Only save if this is a real user with a primary key
            # and not a temporary instance used for validation
            if self.pk:
                self.save(update_fields=["password"])

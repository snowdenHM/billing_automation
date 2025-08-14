from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.translation import gettext_lazy as _

from .managers import UserManager


class User(AbstractUser):
    """Custom user model with email as username.

    We keep the username field but make it optional; the primary login field is email.
    """

    username = models.CharField(max_length=150, unique=False, blank=True, null=True)
    email = models.EmailField(_("email address"), unique=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []  # no username required

    objects = UserManager()

    def __str__(self) -> str:  # pragma: no cover
        return self.email

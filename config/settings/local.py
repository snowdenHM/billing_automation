from .base import *

DEBUG = True
ALLOWED_HOSTS = ["*"]

# Use console email backend locally
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# In local, allow all origins if you prefer quick dev (override as needed)
CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[
    "http://localhost:3000",
    "http://127.0.0.1:3000",
])

# Faster passwords in dev
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]

# DRF browsable API helpful in local
REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"] = (
    "rest_framework.renderers.JSONRenderer",
    "rest_framework.renderers.BrowsableAPIRenderer",
)
from .base import *

DEBUG = True
ALLOWED_HOSTS = ["*"]

# Use console email backend locally
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# In local, allow all origins if you prefer quick dev (override as needed)
CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "https://billmunshi.com",
    "https://www.billmunshi.com",
])
# If you use cookie-based session/CSRF auth:
CSRF_TRUSTED_ORIGINS = [
    "https://billmunshi.com",
    "https://www.billmunshi.com",
    "https://api.billmunshi.com",
]


# Faster passwords in dev
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]

# DRF browsable API helpful in local
REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"] = (
    "rest_framework.renderers.JSONRenderer",
    "rest_framework.renderers.BrowsableAPIRenderer",
)
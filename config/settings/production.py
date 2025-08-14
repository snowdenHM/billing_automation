# config/settings/production.py
from .base import *  # noqa

DEBUG = False

# Hosts / CSRF / CORS
ALLOWED_HOSTS = env.list(
    "ALLOWED_HOSTS",
    default=["billmunshi.com", "www.billmunshi.com"]
)

# When running behind Nginx/Cloudflare, tell Django how to detect HTTPS
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

# Security cookies & headers
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"

SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=31536000)  # 1 year
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)

# CSRF/CORS trusted origins (include scheme!)
CSRF_TRUSTED_ORIGINS = env.list(
    "CSRF_TRUSTED_ORIGINS",
    default=["https://billmunshi.com", "https://www.billmunshi.com"]
)
CORS_ALLOWED_ORIGINS = env.list(
    "CORS_ALLOWED_ORIGINS",
    default=["https://billmunshi.com", "https://www.billmunshi.com"]
)
CORS_ALLOW_CREDENTIALS = True

# Email (override via env in real prod)
EMAIL_BACKEND = env("EMAIL_BACKEND", default="django.core.mail.backends.smtp.EmailBackend")
EMAIL_HOST = env("EMAIL_HOST", default="smtp.sendgrid.net")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="apikey")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="no-reply@billmunshi.com")

# Authentication
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",  # works with custom user if USERNAME_FIELD='email'
]

# Ensure the hashers you use in dev are available in prod.
# If you keep Argon2/Bcrypt here, make sure the packages are installed:
#   pip install argon2-cffi bcrypt
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.BCryptSHA256PasswordHasher",
]

# Keep password validators strict in prod
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# DRF Spectacular & DRF settings (optional, but keeps docs leaner in prod)
REST_FRAMEWORK.setdefault("DEFAULT_RENDERER_CLASSES", ("rest_framework.renderers.JSONRenderer",))

# Logging: raise to WARNING in prod
LOGGING["root"]["level"] = "WARNING"  # noqa
for logger_name in ("django", "django.request", "django.security"):
    LOGGING["loggers"].setdefault(logger_name, {"handlers": ["console"], "level": "WARNING"})  # noqa

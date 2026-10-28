# config/settings/production.py
from django.core.exceptions import ImproperlyConfigured
from .base import *  # noqa

DEBUG = False

# A host missing from this list makes Django reject the request outright with
# "Invalid HTTP_HOST header" (DisallowedHost, HTTP 400) — before any URL
# routing happens, so it looks nothing like a 404 and never reaches a view.
#
# The default below previously omitted ``api.billmunshi.com`` even though
# CSRF_TRUSTED_ORIGINS further down trusts it, so anything calling that
# hostname — the Tally TCP bridge included — was refused. It is listed here
# now, together with the loopback and server-IP entries that base.py already
# carried, so the three lists agree.
#
# NOTE: entries are hostnames only. Django strips the port before matching,
# so a "host:port" entry such as "localhost:5173" matches nothing — plain
# "localhost" is what actually allows the Vite dev server through.
ALLOWED_HOSTS = env.list(
    "ALLOWED_HOSTS",
    default=[
        "billmunshi.com",
        "www.billmunshi.com",
        "api.billmunshi.com",
        "34.51.42.227",
        "127.0.0.1",
        "localhost",
    ],
)

# If you’re behind nginx/Cloudflare
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

# CSRF/CORS trusted origins (include scheme)
# Loopback is deliberately absent. It used to be in this default, and
# with CORS_ALLOW_CREDENTIALS = True that let anything a user happened to
# be running on their own localhost:3000 — a scaffolded dev server, a
# package's dev harness — make credentialed cross-origin calls against
# production with that user's browser. Local origins belong in local.py,
# which has its own list.
CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[
    "https://billmunshi.com",
    "https://www.billmunshi.com",
])
# If you use cookie-based session/CSRF auth:
CSRF_TRUSTED_ORIGINS = [
    "https://billmunshi.com",
    "https://www.billmunshi.com",
    "https://api.billmunshi.com",
]

CORS_ALLOW_CREDENTIALS = True

# Email (override via env in prod)
EMAIL_BACKEND = env("EMAIL_BACKEND", default="django.core.mail.backends.smtp.EmailBackend")
EMAIL_HOST = env("EMAIL_HOST", default="smtp.gmail.com")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")  # Your Gmail address
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")  # Your Gmail app password
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
EMAIL_USE_SSL = env.bool("EMAIL_USE_SSL", default=False)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="no-reply@billmunshi.com")

# Auth backends & password hashers
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
]

# Make sure you installed these:  pip install argon2-cffi bcrypt
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.BCryptSHA256PasswordHasher",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Optional: reduce DRF renderers in prod for smaller responses
REST_FRAMEWORK.setdefault("DEFAULT_RENDERER_CLASSES", ("rest_framework.renderers.JSONRenderer",))

# Full logging config (don’t depend on base.py)
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {
            "format": "[{levelname}] {asctime} {name}: {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "simple",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "WARNING",
    },
    "loggers": {
        "django": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        "django.security": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        "gunicorn.error": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "gunicorn.access": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}

# ---------------------------------------------------------------------------
# Signing key strength
#
# SECRET_KEY is also SIMPLE_JWT["SIGNING_KEY"], so it is what every access
# and refresh token is signed with. PyJWT warns below 32 bytes for
# HMAC-SHA256 (RFC 7518 §3.2), and the development default
# ("django-insecure-change-me") is shorter than that. A short key is a
# brute-forceable key, and forging a token means forging any user.
#
# Fail at startup rather than warn in a log nobody reads.
# ---------------------------------------------------------------------------
if SECRET_KEY.startswith("django-insecure") or len(SECRET_KEY.encode()) < 50:
    raise ImproperlyConfigured(
        "SECRET_KEY is the JWT signing key and must be at least 50 bytes of "
        "random data in production. Generate one with:\n"
        "  python -c \"import secrets; print(secrets.token_urlsafe(64))\""
    )

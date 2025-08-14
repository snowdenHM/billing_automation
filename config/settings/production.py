from .base import *

DEBUG = False

# Always set explicit hosts in prod via env
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["billmunshi.com"])  # change accordingly

# Security hardening
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=31536000)  # 1 year
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)

# Allowed origins for your deployed frontend(s)
CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[])
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

# Email (configure a real backend in prod)
EMAIL_BACKEND = env("EMAIL_BACKEND", default="django.core.mail.backends.smtp.EmailBackend")
EMAIL_HOST = env("EMAIL_HOST", default="smtp.sendgrid.net")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="apikey")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="no-reply@billmunshi.com")

# Database should be provided via DATABASE_URL in env
# Cache/Redis already configured in base via REDIS_URL

# Logging: elevate to WARNING/ERROR in prod
LOGGING["root"]["level"] = "WARNING"


AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",        # REQUIRED for admin
    "allauth.account.auth_backends.AuthenticationBackend",  # if you use allauth
]

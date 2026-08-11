from pathlib import Path

import environ
import os

# ------------------------------------------------------------
# Paths & env
# ------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent.parent
# project root -> BASE_DIR

env = environ.Env()
ENV_FILE = BASE_DIR / ".env"
if ENV_FILE.exists():
    environ.Env.read_env(str(ENV_FILE))

# ------------------------------------------------------------
# Core security
# ------------------------------------------------------------
SECRET_KEY = env("SECRET_KEY", default="django-insecure-change-me")
DEBUG = env.bool("DEBUG", default=False)
# ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["*", "localhost:5173", "34.51.42.227"])
ALLOWED_HOSTS = ["billmunshi.com", "www.billmunshi.com", "api.billmunshi.com", "127.0.0.1", "localhost","34.51.42.227"]
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "your_openai_api_key_here")

# Zoho Books Integration (Server-Based Application)
ZOHO_CLIENT_ID = env("ZOHO_CLIENT_ID", default="")
ZOHO_CLIENT_SECRET = env("ZOHO_CLIENT_SECRET", default="")  
ZOHO_REDIRECT_URL = env("ZOHO_REDIRECT_URL", default="")




# ------------------------------------------------------------
# Applications
# ------------------------------------------------------------
DJANGO_APPS = [
    "unfold",
    "unfold.contrib.filters",
    "unfold.contrib.forms",
    "unfold.contrib.inlines",
    "unfold.contrib.import_export",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "django_filters",
    "drf_spectacular",
    "corsheaders",
    "rest_framework_api_key",
    "waffle",
    "django_rq",
    "anymail",
]

LOCAL_APPS = [
    "apps.common",
    "apps.users",
    "apps.organizations",
    "apps.subscriptions",
    "apps.dashboard",
    "apps.support",
    "apps.api",  # your API router package
]

INTEGRATION_MODULES = [
    "apps.module.tally",
    "apps.module.zoho",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS + INTEGRATION_MODULES

SITE_ID = 1

# ------------------------------------------------------------
# django-unfold admin theme
# ------------------------------------------------------------
UNFOLD = {
    "SITE_TITLE": "Bill Munshi Admin",
    "SITE_HEADER": "Bill Munshi",
    "SITE_URL": "/",
    "SITE_ICON": None,
    "SHOW_HISTORY": True,
    "SHOW_VIEW_ON_SITE": True,
    "THEME": None,  # let user toggle
    # Custom root dashboard — see apps/common/admin_dashboard.py.
    # Renders KPI tiles, recent bills, open support tickets.
    "DASHBOARD_CALLBACK": "apps.common.admin_dashboard.dashboard_callback",
    "COLORS": {
        "primary": {
            "50": "255 247 237",
            "100": "255 237 213",
            "200": "254 215 170",
            "300": "253 186 116",
            "400": "251 146 60",
            "500": "249 115 22",
            "600": "234 88 12",
            "700": "194 65 12",
            "800": "154 52 18",
            "900": "124 45 18",
            "950": "67 20 7",
        },
    },
    "SIDEBAR": {
        "show_search": True,
        "show_all_applications": True,
        "navigation": [
            {
                "title": "Users & Access",
                "items": [
                    {"title": "Users", "icon": "person", "link": "/admin/users/user/"},
                    {"title": "Organizations", "icon": "domain", "link": "/admin/organizations/organization/"},
                ],
            },
            {
                "title": "Tally",
                "items": [
                    {"title": "Tally config", "icon": "settings", "link": "/admin/tally/tallyconfig/"},
                    {"title": "Purchase vouchers", "icon": "receipt", "link": "/admin/tally/tallyvendorbill/"},
                    {"title": "Journal entries", "icon": "book", "link": "/admin/tally/tallyexpensebill/"},
                    {"title": "Payment vouchers", "icon": "payments", "link": "/admin/tally/tallypaymentbill/"},
                    {"title": "Ledgers", "icon": "list", "link": "/admin/tally/ledger/"},
                ],
            },
            {
                "title": "Zoho",
                "items": [
                    {"title": "Vendor bills", "icon": "receipt", "link": "/admin/zoho/zohovendorbill/"},
                    {"title": "Expense bills", "icon": "book", "link": "/admin/zoho/zohoexpensebill/"},
                ],
            },
            {
                "title": "Support",
                "items": [
                    {"title": "Tickets", "icon": "help", "link": "/admin/support/supportticket/"},
                    {"title": "Recipients", "icon": "email", "link": "/admin/support/supportticketrecipient/"},
                ],
            },
            {
                "title": "Ops",
                "items": [
                    {"title": "Background jobs", "icon": "settings_backup_restore", "link": "/admin/rq/"},
                ],
            },
        ],
    },
}

# ------------------------------------------------------------
# Middleware
# ------------------------------------------------------------
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # WhiteNoise MUST sit directly after SecurityMiddleware. Serves every
    # collected asset under STATIC_URL (admin, unfold, drf_spectacular,
    # rest_framework, etc.) directly from the WSGI process — so we don't
    # rely on nginx being configured to alias /static/ → STATIC_ROOT.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "waffle.middleware.WaffleMiddleware",
]

# ------------------------------------------------------------
# URLs & WSGI/ASGI
# ------------------------------------------------------------
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# ------------------------------------------------------------
# Databases (PostgreSQL via DATABASE_URL)
# ------------------------------------------------------------
# Example: DATABASE_URL=postgres://user:pass@localhost:5432/billmunshi
DATABASES = {
    "default": env.db(
        "DATABASE_URL",
        default="sqlite:///" + str(BASE_DIR / "db.sqlite3"),
    )
}

# ------------------------------------------------------------
# Django-RQ Configuration
# ------------------------------------------------------------
RQ_QUEUES = {
    'default': {
        'HOST': env('REDIS_HOST', default='localhost'),
        'PORT': env.int('REDIS_PORT', default=6379),
        'DB': env.int('REDIS_DB', default=0),
        'PASSWORD': env('REDIS_PASSWORD', default=''),
        'DEFAULT_TIMEOUT': 360,
    },
    'high': {
        'HOST': env('REDIS_HOST', default='localhost'),
        'PORT': env.int('REDIS_PORT', default=6379),
        'DB': env.int('REDIS_DB', default=0),
        'PASSWORD': env('REDIS_PASSWORD', default=''),
        'DEFAULT_TIMEOUT': 500,
    },
    'low': {
        'HOST': env('REDIS_HOST', default='localhost'),
        'PORT': env.int('REDIS_PORT', default=6379),
        'DB': env.int('REDIS_DB', default=0),
        'PASSWORD': env('REDIS_PASSWORD', default=''),
        'DEFAULT_TIMEOUT': 500,
    }
}

# ------------------------------------------------------------
# Templates
# ------------------------------------------------------------
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],  # You can store custom HTML templates here
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# ------------------------------------------------------------
# Auth
# ------------------------------------------------------------
AUTH_USER_MODEL = "users.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ------------------------------------------------------------
# Password Hashers
# ------------------------------------------------------------
PASSWORD_HASHERS = [
    'django.contrib.auth.hashers.PBKDF2PasswordHasher',
    'django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher',
    'django.contrib.auth.hashers.BCryptSHA256PasswordHasher',
    'django.contrib.auth.hashers.BCryptPasswordHasher',
]

# ------------------------------------------------------------
# Internationalization
# ------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = env("TIME_ZONE", default="Asia/Kolkata")
USE_I18N = True
USE_TZ = True

# ------------------------------------------------------------
# Static & media
# ------------------------------------------------------------
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"] if (BASE_DIR / "static").exists() else []
# Compressed + hashed static storage via WhiteNoise. Emits assets with
# a content-hash in the filename and long-cache headers — safe to CDN.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        # `CompressedStaticFilesStorage` (not Manifest) — no strict-hash
        # requirement, so a template referencing an asset that wasn't
        # collected won't 500 the request. Swap to
        # `CompressedManifestStaticFilesStorage` once every static ref
        # is known to resolve.
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# ------------------------------------------------------------
# DRF, JWT, Filtering, Schema
# ------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
    "DEFAULT_PAGINATION_CLASS": "apps.common.pagination.DefaultPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_FILTER_BACKENDS": (
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ),
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

SPECTACULAR_SETTINGS = {
    'TITLE': 'BillMunshi API',
    'DESCRIPTION': 'BillMunshi API Documentation',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
    'COMPONENT_SPLIT_REQUEST': True,
    # Disable automatic field introspection for problematic cases
    'DISABLE_ERRORS_AND_WARNINGS': True,
    # Temporarily disable enum name overrides to fix the schema generation issue
    'ENUM_NAME_OVERRIDES': {},
}

# ------------------------------------------------------------
# CORS & CSRF
# ------------------------------------------------------------
CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[])
CORS_ALLOW_CREDENTIALS = True
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

# ------------------------------------------------------------
# Email (welcome, verify, reset password, invites).
#
# Runtime credentials live in the DB (see ``apps.common.models.EmailSettings``)
# so they can be rotated from Django admin without a redeploy. The
# settings below are only used as a fallback when the DB row is empty
# or the ``EmailSettings.is_enabled`` flag is off.
# ------------------------------------------------------------
EMAIL_BACKEND = env(
    "EMAIL_BACKEND",
    default="django.core.mail.backends.console.EmailBackend",
)
# Legacy ``.env`` fallback for the SendGrid key — only used when the
# ``EmailSettings`` row hasn't been populated yet.
SENDGRID_API_KEY = env("SENDGRID_API_KEY", default="")
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="Bill Munshi <support@billmunshi.com>")
SERVER_EMAIL = env("SERVER_EMAIL", default=DEFAULT_FROM_EMAIL)

# Frontend base URL — links in emails (reset password, verify email)
# resolve against this. MUST be set in production.
FRONTEND_URL = env("FRONTEND_URL", default="https://billmunshi.com")
# Public-facing base URL — used in emails/narrations that need a link
# back into the app (bill viewer, admin ticket detail, etc.).
SITE_URL = env("SITE_URL", default=FRONTEND_URL)

# ------------------------------------------------------------
# Cache / Redis (for rate limiting, general cache)
# ------------------------------------------------------------
REDIS_URL = env("REDIS_URL", default="redis://127.0.0.1:6379/1")

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
        # REMOVE the OPTIONS block entirely
        "KEY_PREFIX": "billmunshi",
        "TIMEOUT": 300,
    }
}

# ------------------------------------------------------------
# Logging (concise, JSON‑ready)
# ------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "%(asctime)s [%(levelname)s] %(name)s %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {"handlers": ["console"], "level": "INFO"},
}

# ------------------------------------------------------------
# Django settings niceties
# ------------------------------------------------------------
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = env.bool("SESSION_COOKIE_SECURE", default=False)
CSRF_COOKIE_SECURE = env.bool("CSRF_COOKIE_SECURE", default=False)

# ------------------------------------------------------------
# SimpleJWT (override lifetimes via env as needed)
# ------------------------------------------------------------
from datetime import timedelta

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=env.int("JWT_ACCESS_MINUTES", default=30)),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=env.int("JWT_REFRESH_DAYS", default=7)),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "ALGORITHM": "HS256",
    "SIGNING_KEY": SECRET_KEY,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

# ------------------------------------------------------------
# django-waffle defaults (optional)
# ------------------------------------------------------------
WAFFLE_FLAG_DEFAULT = False
WAFFLE_SWITCH_DEFAULT = False
WAFFLE_SAMPLE_DEFAULT = False

# Default: deny framing. The bill-file viewer route opts out per-view via
# @xframe_options_exempt in apps.common.views.serve_bill_file.
X_FRAME_OPTIONS = 'DENY'

# ------------------------------------------------------------
# Upload limits (applies to multipart parsing and request body)
# ------------------------------------------------------------
# Override via env: BILL_MAX_UPLOAD_MB (defaults to 25 MB)
_MAX_UPLOAD_MB = env.int("BILL_MAX_UPLOAD_MB", default=25)
DATA_UPLOAD_MAX_MEMORY_SIZE = _MAX_UPLOAD_MB * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = _MAX_UPLOAD_MB * 1024 * 1024
BILL_MAX_UPLOAD_BYTES = DATA_UPLOAD_MAX_MEMORY_SIZE

# ------------------------------------------------------------
# Google reCAPTCHA v2 ("I'm not a robot")
# ------------------------------------------------------------
# Guards the two public, unauthenticated forms: /book-demo and
# /auth/register. Setting the secret switches verification on — the same
# convention SENDGRID_API_KEY uses — so dev and CI need no extra flag.
RECAPTCHA_SECRET_KEY = env("RECAPTCHA_SECRET_KEY", default="")

# Tri-state override of the auto-detection above. Leave the env var unset
# for "on when a secret is configured"; set it to force either answer.
_RECAPTCHA_ENABLED_OVERRIDE = env("RECAPTCHA_ENABLED", default="").strip().lower()
RECAPTCHA_ENABLED = (
    None if not _RECAPTCHA_ENABLED_OVERRIDE
    else _RECAPTCHA_ENABLED_OVERRIDE in ("1", "true", "yes", "on")
)

# Seconds to wait on Google's siteverify endpoint. It sits in the request
# path of a form submit, so keep it short.
RECAPTCHA_TIMEOUT = env.float("RECAPTCHA_TIMEOUT", default=5.0)

# When siteverify is unreachable we reject the submission. Set this to
# True to accept instead — trades spam exposure for uptime.
RECAPTCHA_FAIL_OPEN = env.bool("RECAPTCHA_FAIL_OPEN", default=False)

# ------------------------------------------------------------
# Trash retention
# ------------------------------------------------------------
# Days a bill stays recoverable in Trash before `purge_trashed_bills`
# destroys the row and its uploaded file for good. Lowering this makes
# already-trashed bills expire sooner, since the window is measured from
# each bill's own `deleted_at`.
TRASH_RETENTION_DAYS = env.int("TRASH_RETENTION_DAYS", default=30)

# ------------------------------------------------------------
# OpenAI client
# ------------------------------------------------------------
OPENAI_REQUEST_TIMEOUT = env.float("OPENAI_REQUEST_TIMEOUT", default=60.0)
OPENAI_MAX_RETRIES = env.int("OPENAI_MAX_RETRIES", default=2)

# ------------------------------------------------------------
# Upload screening — "is this actually a bill?"
# ------------------------------------------------------------
# Runs one vision call per uploaded file BEFORE the Draft row is created, so
# a random PDF never becomes a bill. Costs an extra API call and a couple of
# seconds per file; set to False to skip it entirely.
BILL_DOCUMENT_VALIDATION_ENABLED = env.bool(
    "BILL_DOCUMENT_VALIDATION_ENABLED", default=True,
)
# Only refuse a file when the model is at least this confident it is not a
# bill. Raise it to reject less (fewer false rejections, more junk gets in);
# lower it to reject more aggressively.
BILL_DOCUMENT_VALIDATION_MIN_CONFIDENCE = env.float(
    "BILL_DOCUMENT_VALIDATION_MIN_CONFIDENCE", default=0.7,
)
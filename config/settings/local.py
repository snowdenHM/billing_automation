from .base import *
import os

# Fix for macOS fork() issue with RQ workers
# This prevents "objc[PID]: +[NSNumber initialize] may have been in progress" crash
os.environ['OBJC_DISABLE_INITIALIZE_FORK_SAFETY'] = 'YES'

DEBUG = True
ALLOWED_HOSTS = ["*"]

# Allow iframe embedding for file viewer
X_FRAME_OPTIONS = 'SAMEORIGIN'

# Ensure media files can be served in iframes
SECURE_FRAME_DENY = False

# Use console email backend locally
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# In local, allow all origins if you prefer quick dev (override as needed)
CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:5173", 
    "http://127.0.0.1:5173",
    "https://billmunshi.com",
    "https://www.billmunshi.com",
])
# If you use cookie-based session/CSRF auth:
CSRF_TRUSTED_ORIGINS = [
    "https://billmunshi.com",
    "https://www.billmunshi.com", 
    "https://api.billmunshi.com",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]

# Allow all CORS headers for local development
CORS_ALLOW_ALL_ORIGINS = True
CORS_ALLOW_CREDENTIALS = True

# Remove X-Frame-Options middleware for local development to allow PDF viewing
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    # "django.middleware.clickjacking.XFrameOptionsMiddleware",  # Disabled for local PDF viewing
    "waffle.middleware.WaffleMiddleware",
]


# Faster passwords in dev
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]

# DRF browsable API helpful in local
REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"] = (
    "rest_framework.renderers.JSONRenderer",
    "rest_framework.renderers.BrowsableAPIRenderer",
)
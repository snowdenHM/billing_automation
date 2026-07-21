"""
Shared utility functions used across the project.
"""
import logging
import re
from datetime import datetime

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection, send_mail
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string
from django.template import TemplateDoesNotExist

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def send_simple_email(subject: str, message: str, to_email: str, from_email: str | None = None):
    """Small helper to send a text email; uses configured EMAIL_BACKEND."""
    if not from_email:
        from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@local")
    send_mail(subject, message, from_email, [to_email], fail_silently=False)


def _resolve_email_connection(cfg):
    """Return a DRF-style email connection matching the runtime config.

    When SendGrid is enabled we build an anymail SendGrid backend with the
    DB-provided API key. Otherwise Django's ``get_connection`` returns the
    ``EMAIL_BACKEND`` configured in settings (console in dev, SMTP in
    prod fallback).
    """
    if cfg.use_sendgrid:
        return get_connection(
            backend="anymail.backends.sendgrid.EmailBackend",
            api_key=cfg.sendgrid_api_key,
        )
    return get_connection()


def send_templated_email(
    subject: str,
    template_base: str,
    to_email: str,
    context: dict | None = None,
    from_email: str | None = None,
    reply_to: list[str] | None = None,
):
    """Render ``emails/<template_base>.{txt,html}`` and send a multipart email.

    ``template_base`` is the shared basename of the two template files —
    e.g. ``"welcome"`` renders both ``templates/emails/welcome.txt`` and
    ``templates/emails/welcome.html``. The HTML alternative is attached
    only when the file exists; otherwise the plain-text body is sent
    alone so misconfigured templates never silently drop the email.

    Sender identity + SendGrid credentials are loaded at runtime from
    the DB-backed ``EmailSettings`` singleton (see
    ``apps.common.email_config``), so operators can rotate the API key
    from Django admin without a redeploy.

    Failures are logged and re-raised so the calling view/task can
    surface them; the mail helper does NOT swallow exceptions.
    """
    from apps.common.email_config import get_email_config

    context = context or {}
    cfg = get_email_config()

    # Explicit ``from_email`` argument still wins so ad-hoc sends can
    # override the DB default when needed.
    if not from_email:
        from_email = cfg.formatted_from

    reply_to_addrs = reply_to or ([cfg.reply_to] if cfg.reply_to else None)

    text_body = render_to_string(f"emails/{template_base}.txt", context)
    try:
        html_body = render_to_string(f"emails/{template_base}.html", context)
    except TemplateDoesNotExist:
        html_body = None

    connection = _resolve_email_connection(cfg)
    email = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=from_email,
        to=[to_email],
        reply_to=reply_to_addrs,
        connection=connection,
    )
    if html_body:
        email.attach_alternative(html_body, "text/html")

    try:
        email.send(fail_silently=False)
        logger.info(
            "Sent '%s' email to %s (template=%s, sendgrid=%s)",
            subject, to_email, template_base, cfg.use_sendgrid,
        )
    except Exception as exc:
        logger.exception("Failed to send '%s' email to %s: %s", subject, to_email, exc)
        raise


# ---------------------------------------------------------------------------
# Organization resolution
# ---------------------------------------------------------------------------

def get_organization_from_request(request, org_id=None, **kwargs):
    """
    Resolve the Organization from the current request.

    Resolution order:
      1. Explicit *org_id* — the requester must be authorized for it via:
         (a) ``request.organization`` already populated by an API-key
             permission class for the *same* org_id, OR
         (b) ``request.auth`` linked to an ``OrganizationAPIKey`` for the
             same org, OR
         (c) the authenticated user is staff/superuser, OR
         (d) the authenticated user has an active ``OrgMembership`` for it.
         Raises ``PermissionDenied`` otherwise.
      2. ``request.organization`` set by an API-key permission class.
      3. ``request.auth`` linked to an OrganizationAPIKey.
      4. First active OrgMembership of the authenticated user.

    Returns ``None`` when no organisation can be determined.
    """
    from rest_framework.exceptions import PermissionDenied

    from apps.organizations.models import Organization, OrganizationAPIKey

    user = getattr(request, "user", None)

    # 1. From explicit org_id — verify the caller is authorized for it.
    _org_id = org_id or kwargs.get("org_id")
    if _org_id:
        organization = get_object_or_404(Organization, id=_org_id)

        # (a) API-key path: the permission class already validated the key
        # and stamped ``request.organization``. Accept it if it matches.
        request_org = getattr(request, "organization", None)
        if request_org and str(request_org.id) == str(organization.id):
            return organization

        # (b) DRF API-key auth path: ``request.auth`` is an APIKey instance
        # linked to an OrganizationAPIKey. Accept if it belongs to this org.
        if hasattr(request, "auth") and request.auth:
            try:
                org_api_key = OrganizationAPIKey.objects.get(api_key=request.auth)
                if str(org_api_key.organization_id) == str(organization.id):
                    return organization
            except OrganizationAPIKey.DoesNotExist:
                pass

        # (c) Staff / superuser bypass.
        if user and user.is_authenticated and (user.is_staff or user.is_superuser):
            return organization

        # (d) Bearer-token path: the authenticated user must have an active
        # membership in the org. This is the original IDOR guard for the
        # user-session flow.
        if (
            user
            and user.is_authenticated
            and organization.memberships.filter(user=user, is_active=True).exists()
        ):
            return organization

        raise PermissionDenied("You do not have access to this organization.")

    # 2. Injected by permission class (e.g. OrganizationAPIKeyOrBearerToken)
    if hasattr(request, "organization") and request.organization:
        return request.organization

    # 3. Via DRF API-key auth object
    if hasattr(request, "auth") and request.auth:
        try:
            return OrganizationAPIKey.objects.get(api_key=request.auth).organization
        except OrganizationAPIKey.DoesNotExist:
            logger.warning("request.auth is not linked to any OrganizationAPIKey")

    # 4. Fallback to user membership
    if user and user.is_authenticated and hasattr(user, "memberships"):
        membership = user.memberships.filter(is_active=True).first()
        if membership:
            return membership.organization

    return None


# ---------------------------------------------------------------------------
# String similarity (used for duplicate detection)
# ---------------------------------------------------------------------------

def calculate_string_similarity(str1, str2):
    """
    Calculate similarity between two strings using enhanced logic
    tailored for Indian business names.

    Returns a float between 0.0 and 1.0.
    """
    if not str1 or not str2:
        return 0.0

    str1_clean = str1.lower().strip()
    str2_clean = str2.lower().strip()

    if str1_clean == str2_clean:
        return 1.0

    # Common business abbreviations for Indian companies
    abbreviations = {
        "ltd": "limited",
        "pvt": "private",
        "llp": "limited liability partnership",
        "co": "company",
        "corp": "corporation",
        "inc": "incorporated",
        "enterprises": "ent",
        "industries": "ind",
        "services": "svc",
        "technologies": "tech",
        "systems": "sys",
        "solutions": "sol",
    }

    for abbrev, full in abbreviations.items():
        str1_clean = str1_clean.replace(full, abbrev).replace(abbrev, abbrev)
        str2_clean = str2_clean.replace(full, abbrev).replace(abbrev, abbrev)

    # Word-based Jaccard similarity
    str1_words = set(str1_clean.split())
    str2_words = set(str2_clean.split())

    if not str1_words or not str2_words:
        return 0.0

    intersection = str1_words & str2_words
    union = str1_words | str2_words
    word_similarity = len(intersection) / len(union) if union else 0.0

    # Character-level overlap
    max_len = max(len(str1_clean), len(str2_clean))
    min_len = min(len(str1_clean), len(str2_clean))

    if max_len == 0:
        return 1.0

    common_chars = sum(
        min(str1_clean.count(c), str2_clean.count(c)) for c in set(str1_clean)
    )
    char_similarity = (2 * common_chars) / (len(str1_clean) + len(str2_clean))

    length_similarity = min_len / max_len

    # Weighted combination
    final_similarity = (
        word_similarity * 0.6
        + char_similarity * 0.3
        + length_similarity * 0.1
    )
    return min(final_similarity, 1.0)


# ---------------------------------------------------------------------------
# Safe data access helpers
# ---------------------------------------------------------------------------

def safe_get_nested(data, keys, default=None):
    """Safely get nested dictionary value by following a sequence of keys."""
    try:
        current = data
        for key in keys:
            if isinstance(current, dict) and key in current:
                current = current[key]
            else:
                return default
        return current
    except (TypeError, KeyError):
        return default


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

def parse_bill_date(date_string):
    """Parse bill date string with multiple format support.

    Tries common Indian date formats first, then ISO.
    Returns ``datetime.date`` or ``None``.
    """
    if not date_string:
        return None

    date_formats = [
        '%d-%m-%Y',
        '%Y-%m-%d',
        '%d/%m/%Y',
        '%Y/%m/%d',
        '%d.%m.%Y',
        '%Y.%m.%d',
    ]

    for fmt in date_formats:
        try:
            return datetime.strptime(str(date_string), fmt).date()
        except ValueError:
            continue

    logger.warning(f"Could not parse date: {date_string}")
    return None


# ---------------------------------------------------------------------------
# Company name normalisation
# ---------------------------------------------------------------------------

def normalize_company_name(name):
    """Normalize company name for comparison / matching.

    Handles year suffixes, punctuation, and common Indian business suffixes.
    """
    if not name:
        return ""

    normalized = name.strip()

    # Remove year patterns like (2025-26), (FY25)
    normalized = re.sub(r'\s*\([0-9]{4}[-/][0-9]{2,4}\)', '', normalized)
    normalized = re.sub(r'\s*\(FY[0-9]{2}\)', '', normalized)

    # Normalize punctuation and spacing
    normalized = re.sub(r'[&]+', '&', normalized)
    normalized = re.sub(r'\s*&\s*', ' & ', normalized)
    normalized = re.sub(r'\.+', '.', normalized)
    normalized = re.sub(r'\s*\.\s*', '. ', normalized)

    # Handle common business suffixes
    business_suffixes = [
        'Mfg.Co.', 'Mfg Co', 'Manufacturing Co', 'Mfg. Co.', 'Mfg.Co',
        'Pvt Ltd', 'Pvt. Ltd.', 'Private Limited', 'Ltd', 'Ltd.',
        'LLC', 'LLP', 'Co.', 'Co', 'Company', 'Corp', 'Corporation',
        'Inc', 'Inc.', 'Industries', 'Enterprises', 'Trading', 'Traders',
    ]

    for suffix in business_suffixes:
        pattern = r'\b' + re.escape(suffix) + r'\b'
        if re.search(pattern, normalized, re.IGNORECASE):
            if 'Mfg' in suffix:
                normalized = re.sub(pattern, 'Mfg. Co.', normalized, flags=re.IGNORECASE)
            elif 'Pvt' in suffix and 'Ltd' in suffix:
                normalized = re.sub(pattern, 'Pvt. Ltd.', normalized, flags=re.IGNORECASE)
            elif suffix in ['Ltd', 'Ltd.']:
                normalized = re.sub(pattern, 'Ltd.', normalized, flags=re.IGNORECASE)

    normalized = re.sub(r'\s+', ' ', normalized).strip()
    return normalized


def normalize_company_name_enhanced(name):
    """Enhanced company name normalization for Indian businesses.

    Strips all business suffixes and special characters for fuzzy comparison.
    """
    if not name:
        return ""

    normalized = name.strip()

    # Remove year patterns
    normalized = re.sub(r'\s*\([0-9]{4}[-/][0-9]{2,4}\)', '', normalized)
    normalized = re.sub(r'\s*\(FY[0-9]{2}\)', '', normalized)

    business_suffixes = [
        'Private Limited', 'Pvt Ltd', 'Pvt. Ltd.', 'Ltd', 'Ltd.',
        'Limited Liability Partnership', 'LLP', 'LLC',
        'Company', 'Co.', 'Co', 'Corporation', 'Corp', 'Inc', 'Inc.',
        'Enterprises', 'Industries', 'Trading', 'Traders', 'Services',
        'Technologies', 'Tech', 'Systems', 'Solutions',
    ]

    for suffix in business_suffixes:
        normalized = re.sub(r'\b' + re.escape(suffix) + r'\b', '', normalized, flags=re.IGNORECASE)

    normalized = re.sub(r'\s+', ' ', normalized).strip()
    normalized = re.sub(r'[^\w\s]', '', normalized)  # Remove special chars
    return normalized


# ---------------------------------------------------------------------------
# GST helpers
# ---------------------------------------------------------------------------

def calculate_gst_rate(amount, igst_val, cgst_val, sgst_val):
    """Calculate GST rate percentage based on tax amounts and item amount.

    Returns a string representation of the closest standard GST rate.
    """
    try:
        if amount <= 0:
            return "0"

        total_tax = igst_val + cgst_val + sgst_val
        if total_tax <= 0:
            return "0"

        # taxable_amount = amount - total_tax (reverse calculation)
        taxable_amount = amount - total_tax
        if taxable_amount <= 0:
            taxable_amount = amount  # Fallback

        gst_rate = (total_tax / taxable_amount) * 100

        standard_rates = [0, 5, 12, 18, 28]
        closest_rate = min(standard_rates, key=lambda x: abs(x - gst_rate))
        return str(closest_rate)

    except Exception as e:
        logger.error(f"Error calculating GST rate: {str(e)}")
        return "18"  # Default fallback


def normalize_product_gst(gst_value):
    """Normalize GST value to match GST_CHOICES format.

    Returns one of: ``"0%"``, ``"5%"``, ``"12%"``, ``"18%"``, ``"28%"``,
    ``"Exempted"``, ``"N/A"``.
    """
    if not gst_value:
        return "N/A"

    gst_str = str(gst_value).strip().upper()

    if "EXEMPT" in gst_str:
        return "Exempted"
    if gst_str in ("N/A", "NA", "NONE", ""):
        return "N/A"

    try:
        numeric_str = gst_str.replace('%', '').replace('GST', '').strip()
        gst_numeric = float(numeric_str)
        standard_rates = [0, 5, 12, 18, 28]
        closest_rate = min(standard_rates, key=lambda x: abs(x - gst_numeric))
        return f"{closest_rate}%"
    except (ValueError, AttributeError):
        logger.warning(f"Could not normalize GST value: {gst_value}, defaulting to N/A")
        return "N/A"


# ---------------------------------------------------------------------------
# Numeric string coercion
# ---------------------------------------------------------------------------

def safe_numeric_string(value, default="0"):
    """Convert *value* to a numeric string, falling back to *default*.

    Used when building Zoho bill objects from AI-analysed data where
    values may be ``None``, empty strings, or non-numeric.
    """
    try:
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return str(value)
        float(str(value))  # validate it's numeric
        return str(value)
    except (ValueError, TypeError):
        logger.warning("Invalid numeric value: %s, using default: %s", value, default)
        return default
# apps/module/zoho/views/helpers.py
"""
Shared zoho helpers used by credentials, sync, vendor, expense, and journal views.
"""
import logging
from decimal import Decimal
import os

import requests
from django.db.models.functions import Lower
from django.utils import timezone

from ..models import ZohoCredentials, ZohoVendor, ZohoCustomer, ZohoChartOfAccount, ZohoTaxes
from apps.common.utils import (
    get_organization_from_request,  # noqa: F401 – re-exported
    calculate_string_similarity,
    normalize_company_name_enhanced,  # noqa: F401 – re-exported from common
    safe_numeric_string,  # noqa: F401 – re-exported from common
)
from apps.common.services.bill_analysis import (
    analyze_bill_bytes,
    get_vendor_bill_prompt,
)

logger = logging.getLogger(__name__)

# Seconds. An HTTP call with no timeout pins a worker indefinitely.
ZOHO_HTTP_TIMEOUT = 30


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def get_zoho_credentials(organization):
    """Get or create valid Zoho credentials for organization using environment variables."""
    client_id = os.getenv("ZOHO_CLIENT_ID")
    client_secret = os.getenv("ZOHO_CLIENT_SECRET")
    redirect_url = os.getenv("ZOHO_REDIRECT_URL")

    if not all([client_id, client_secret, redirect_url]):
        raise ValueError(
            "Zoho environment variables (ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REDIRECT_URL) are not configured"
        )

    try:
        credentials, created = ZohoCredentials.objects.get_or_create(
            organization=organization,
            defaults={
                "clientId": client_id,
                "clientSecret": client_secret,
                "redirectUrl": redirect_url,
                "organisationId": "",
            },
        )

        if not created:
            credentials.clientId = client_id
            credentials.clientSecret = client_secret
            credentials.redirectUrl = redirect_url
            credentials.save(update_fields=["clientId", "clientSecret", "redirectUrl"])

        if credentials.accessToken and not credentials.is_token_valid():
            if not credentials.refresh_token():
                credentials.accessToken = None
                credentials.refreshToken = None
                credentials.is_connected = False
                credentials.save(update_fields=["accessToken", "refreshToken", "is_connected"])

        return credentials
    except Exception as e:
        logger.error(f"Error managing Zoho credentials: {str(e)}")
        raise ValueError(f"Failed to get Zoho credentials: {str(e)}")


def refresh_zoho_access_token(current_token):
    """Refresh Zoho access token using refresh token."""
    refresh_token = current_token.refreshToken
    client_id = current_token.clientId
    client_secret = current_token.clientSecret

    url = (
        f"https://accounts.zoho.in/oauth/v2/token?"
        f"refresh_token={refresh_token}&client_id={client_id}"
        f"&client_secret={client_secret}&grant_type=refresh_token"
    )

    try:
        response = requests.post(url, timeout=ZOHO_HTTP_TIMEOUT)
        if response.status_code == 200:
            new_access_token = response.json().get("access_token")
            current_token.accessToken = new_access_token
            current_token.save()
            return new_access_token
        else:
            logger.error(f"Failed to refresh token: {response.status_code} - {response.text}")
            return None
    except Exception as e:
        logger.error(f"Error refreshing token: {str(e)}")
        return None


# ---------------------------------------------------------------------------
# API request
# ---------------------------------------------------------------------------

def make_zoho_api_request(credentials, endpoint, method="GET", data=None):
    """Make authenticated request to Zoho API with proactive token refresh support."""
    if credentials.token_expiry and timezone.now() >= (credentials.token_expiry - timezone.timedelta(minutes=5)):
        if credentials.refreshToken:
            credentials.refresh_token()

    def _make_request(access_token):
        headers = {"Authorization": f"Zoho-oauthtoken {access_token}", "Content-Type": "application/json"}
        separator = "&" if "?" in endpoint else "?"
        url = f"https://www.zohoapis.in/books/v3/{endpoint}{separator}organization_id={credentials.organisationId}"

        if method == "GET":
            response = requests.get(url, headers=headers, timeout=30)
        elif method == "POST":
            response = requests.post(url, headers=headers, json=data, timeout=30)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")
        return response

    try:
        response = _make_request(credentials.accessToken)

        if response.status_code == 401:
            if not credentials.refreshToken:
                raise ValueError("Access token expired and no refresh token available. Please re-authenticate.")
            success = credentials.refresh_token()
            if not success:
                raise ValueError("Failed to refresh access token. Please re-authenticate.")
            response = _make_request(credentials.accessToken)

        response.raise_for_status()
        return response.json()

    except requests.RequestException as e:
        logger.error(f"Zoho API request failed: {str(e)}")
        if hasattr(e, "response") and e.response is not None:
            try:
                logger.error(f"Zoho API error response: {e.response.json()}")
            except Exception:
                logger.error(f"Zoho API error response (raw): {e.response.text}")
        raise


# ---------------------------------------------------------------------------
# Vendor / ledger lookup
# ---------------------------------------------------------------------------

def find_zoho_vendor_ledger_enhanced(company_name, organization, vendor_gst=None):
    """Find matching vendor using GST first, then enhanced name matching."""
    return _find_contact_enhanced(
        ZohoVendor, company_name, organization, vendor_gst, label="vendor",
    )


def _find_contact_enhanced(model, company_name, organization, gst=None, *, label="contact"):
    """GST-first, then name-based lookup of a Zoho contact.

    One body for both :class:`ZohoVendor` and :class:`ZohoCustomer`: the
    matching rules are a property of how Indian invoices identify a party,
    not of which side of the ledger the party sits on.
    """
    try:
        # 1. Exact GST match (most reliable)
        if gst and len(gst.strip()) >= 10:
            hit = model.objects.filter(organization=organization, gstNo=gst.strip()).first()
            if hit:
                logger.info("Found %s by exact GST match: %s", label, hit.companyName)
                return hit

        if not company_name:
            return None

        normalized_name = normalize_company_name_enhanced(company_name)

        # 2. Exact normalised name (case-insensitive)
        hit = model.objects.filter(organization=organization).annotate(
            lower_name=Lower("companyName"),
        ).filter(lower_name=normalized_name.lower()).first()
        if hit:
            logger.info("Found %s by exact name match: %s", label, hit.companyName)
            return hit

        # 3. Contains match
        hit = model.objects.filter(
            organization=organization, companyName__icontains=normalized_name,
        ).first()
        if hit:
            logger.info("Found %s by partial name match: %s", label, hit.companyName)
            return hit

        # 4. Fuzzy matching
        best_match, best_score = None, 0
        for candidate in model.objects.filter(organization=organization):
            score = calculate_string_similarity(
                normalized_name.lower(),
                normalize_company_name_enhanced(candidate.companyName).lower(),
            )
            if score > best_score and score > 0.8:
                best_match, best_score = candidate, score

        if best_match:
            logger.info("Found %s by fuzzy match (%.2f): %s", label, best_score, best_match.companyName)
            return best_match

        logger.warning("No %s found for: %s (GST: %s)", label, company_name, gst)
        return None

    except Exception as e:
        logger.error("Error finding %s: %s", label, e)
        return None


def find_zoho_customer_ledger_enhanced(company_name, organization, customer_gst=None):
    """Find a matching :class:`ZohoCustomer` — the sales-side mirror of
    :func:`find_zoho_vendor_ledger_enhanced`."""
    return _find_contact_enhanced(
        ZohoCustomer, company_name, organization, customer_gst, label="customer",
    )


def find_appropriate_zoho_coa_ledger(organization, item_description, ledger_type="vendor"):
    """Find appropriate chart of accounts ledger based on item description.

    ``ledger_type`` may be ``'vendor'``, ``'expense'``, or ``'journal'``
    — the keyword mappings are combined so a single function covers all cases.
    """
    try:
        all_coa = ZohoChartOfAccount.objects.filter(organization=organization)
        if not all_coa.exists():
            logger.warning(f"No Chart of Accounts found for organization {organization.id}")
            return None

        item_lower = (item_description or "").lower().strip()

        # Combined keyword mappings (vendor + expense + journal)
        keyword_mappings = {
            # Expense / purchase categories
            "purchase": ["purchase", "buying", "procurement"],
            "expense": ["expense", "cost", "expenditure"],
            "travel": ["travel expenses", "travelling", "conveyance"],
            "food": ["food", "meals", "refreshment", "pantry", "restaurant"],
            "office": ["office expenses", "office supplies", "stationery", "administrative", "admin"],
            "rent": ["rent", "rental"],
            "utilities": ["electricity", "water", "utilities", "telephone", "mobile"],
            "salaries": ["salary", "wages", "payroll"],
            "professional": ["professional fees", "consultant", "audit", "legal"],
            "marketing": ["marketing", "advertising", "promotion"],
            "repairs": ["repairs", "maintenance", "ami", "service"],
            "depreciation": ["depreciation", "amortization"],
            "insurance": ["insurance", "premium"],
            "fuel": ["fuel", "petrol", "diesel", "gas", "energy", "power"],
            "printing": ["printing", "photocopy"],
            "postage": ["postage", "courier"],
            # Income categories (journal)
            "sales": ["sales", "revenue", "income"],
            "interest": ["interest income", "interest earned"],
            # Asset / Liability (journal)
            "cash": ["cash", "bank"],
            "receivable": ["receivable", "debtors"],
            "payable": ["payable", "creditors"],
        }

        for _category, keywords in keyword_mappings.items():
            for kw in keywords:
                if kw in item_lower:
                    match = all_coa.filter(accountName__icontains=kw).first()
                    if match:
                        logger.info(f"Found CoA by keyword '{kw}': {match.accountName}")
                        return match

        # Vendor-specific fallback: most commonly used CoA
        if ledger_type == "vendor":
            from django.db import models as _m
            from ..models import VendorZohoProduct

            common = (
                VendorZohoProduct.objects.filter(
                    zohoBill__organization=organization,
                    chart_of_accounts__isnull=False,
                )
                .values("chart_of_accounts")
                .annotate(usage_count=_m.Count("chart_of_accounts"))
                .order_by("-usage_count")
                .first()
            )
            if common:
                coa = ZohoChartOfAccount.objects.filter(id=common["chart_of_accounts"], organization=organization).first()
                if coa:
                    logger.info(f"Using most common CoA: {coa.accountName}")
                    return coa

        # Generic fallback
        for kw in ("expense", "general"):
            fallback = all_coa.filter(accountName__icontains=kw).first()
            if fallback:
                logger.info(f"Using default CoA: {fallback.accountName}")
                return fallback

        first = all_coa.first()
        logger.warning(f"No specific match, using first available CoA: {first.accountName if first else 'None'}")
        return first

    except Exception as e:
        logger.error(f"Error finding appropriate CoA ledger: {str(e)}")
        return None


def find_appropriate_zoho_tax_ledger(organization, tax_type, tax_amount, rate=None):
    """The Zoho tax to put on a line, matched on RATE first.

    *rate* is the line's own GST rate as a percentage (5, 18, ...).

    This used to match on the tax NAME alone and return ``.first()``,
    which could not tell "CGST 2.5%" from "CGST 9%" — an organisation
    with several slabs got whichever row the database happened to
    return. It was also called once per bill with the bill-level totals,
    so every line on a mixed-rate bill was given the same tax and an
    exempt line was taxed.

    That matters more here than it would in Tally: Zoho Books recomputes
    the tax from the ``tax_id`` we send, so a wrong rate becomes a wrong
    invoice in the client's own books.

    ``tax_amount`` is kept for callers that still pass it positionally;
    the rate is what actually selects.
    """
    try:
        # A line that is genuinely zero-rated must not be given a taxed
        # ledger. Returning None leaves it untaxed, which is correct.
        if rate is not None and float(rate) == 0:
            return None

        if rate is not None:
            target = Decimal(str(rate))
            # Zoho splits an intra-state rate across CGST and SGST rows,
            # each at half, so a 5% line matches a 2.5% row. Try the
            # exact rate first, then the half.
            wanted = [target, target / 2] if tax_type in ("cgst", "sgst") else [target]

            # Name first, across every candidate rate. Matching the rate
            # without the name would let an 18% CGST line pick up an
            # "IGST 18%" row, which posts the wrong tax head.
            for value in wanted:
                hit = (
                    ZohoTaxes.objects
                    .filter(organization=organization, percentage=value)
                    .filter(taxName__icontains=tax_type)
                    .first()
                )
                if hit:
                    logger.info(
                        "Matched %s tax by rate %s%%: %s", tax_type, value, hit.taxName
                    )
                    return hit

            # A nameless match is only safe for IGST, which is a single
            # row at the full rate. For CGST/SGST it would be a coin
            # flip between the two heads and the wrong one posts wrongly.
            if tax_type == "igst":
                hit = (
                    ZohoTaxes.objects
                    .filter(organization=organization, percentage=target)
                    .first()
                )
                if hit:
                    logger.info("Matched igst tax by rate %s%%: %s", target, hit.taxName)
                    return hit
            logger.warning(
                "No Zoho tax at %s%% for %s in org %s — leaving the line untaxed "
                "rather than guessing a rate",
                rate, tax_type, organization.id,
            )
            # Deliberately no name-based fallback here. Guessing a rate
            # writes a wrong number into the client's books; an empty
            # tax is visible and gets fixed on the verify screen.
            return None

        tax_patterns = {
            "igst": ["igst", "integrated gst", "integrated goods", "inter state"],
            "cgst": ["cgst", "central gst", "central goods"],
            "sgst": ["sgst", "state gst", "state goods"],
            "gst": ["gst", "goods and service"],
            "vat": ["vat", "value added"],
        }

        patterns = tax_patterns.get(tax_type.lower(), [tax_type.lower()])

        # Exact then contains
        for pattern in patterns:
            hit = ZohoTaxes.objects.filter(organization=organization, taxName__iexact=pattern).first()
            if hit:
                logger.info(f"Found exact match {tax_type} tax: {hit.taxName}")
                return hit

        for pattern in patterns:
            hit = ZohoTaxes.objects.filter(organization=organization, taxName__icontains=pattern).first()
            if hit:
                logger.info(f"Found {tax_type} tax by pattern: {hit.taxName}")
                return hit

        # Vendor-specific fallback: most commonly used tax
        from django.db import models as _m
        from ..models import VendorZohoProduct

        common = (
            VendorZohoProduct.objects.filter(
                zohoBill__organization=organization,
                taxes__isnull=False,
            )
            .values("taxes")
            .annotate(usage_count=_m.Count("taxes"))
            .order_by("-usage_count")
            .first()
        )
        if common:
            tax = ZohoTaxes.objects.filter(id=common["taxes"], organization=organization).first()
            if tax:
                logger.info(f"Using most common tax: {tax.taxName}")
                return tax

        logger.warning(f"No appropriate {tax_type} tax ledger found")
        return None

    except Exception as e:
        logger.error(f"Error finding {tax_type} tax ledger: {str(e)}")
        return None


# ---------------------------------------------------------------------------
# OpenAI bill analysis
# ---------------------------------------------------------------------------

_ANALYSIS_FALLBACK = {
    "invoiceNumber": "", "dateIssued": "", "dueDate": "",
    "from": {"name": "", "address": "", "gst_number": ""},
    "to": {"name": "", "address": "", "gst_number": ""},
    "items": [], "total": 0, "igst": 0, "cgst": 0, "sgst": 0,
}


def analyze_zoho_bill(file_content, file_extension, label="zoho"):
    """Analyse a bill image/PDF via OpenAI — shared by vendor/expense/journal views."""
    logger.info("Starting %s bill analysis for file type: %s", label, file_extension)
    try:
        return analyze_bill_bytes(file_content, file_extension, get_vendor_bill_prompt())
    except Exception as e:
        logger.error("%s bill analysis failed: %s", label.capitalize(), e)
        return {**_ANALYSIS_FALLBACK, "error": f"Analysis failed: {e}"}

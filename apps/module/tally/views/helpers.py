# apps/module/tally/views/helpers.py
"""
Shared helpers used across tally views.
Kept here to avoid circular imports between view modules.
"""
import logging
import re

from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from apps.common.permissions import OrganizationAPIKeyOrBearerToken  # noqa: F401 – re-exported
from apps.common.utils import normalize_company_name, calculate_string_similarity
from apps.common.converters import safe_float_convert, safe_int_convert

logger = logging.getLogger(__name__)


class NoPagination(PageNumberPagination):
    """Custom pagination that returns all results but maintains the paginated response format."""

    def paginate_queryset(self, queryset, request, view=None):
        self.queryset = queryset
        return list(queryset)

    def get_paginated_response(self, data):
        return Response({"count": len(data), "next": None, "previous": None, "results": data})


def clean_decimal_value(value_str):
    """Clean decimal value by removing commas and converting to proper decimal format."""
    if not value_str or value_str in ("", "0", None):
        return "0.00"
    try:
        cleaned = str(value_str).replace(",", "").strip()
        if not cleaned:
            return "0.00"
        return f"{float(cleaned):.2f}"
    except (ValueError, TypeError) as e:
        logger.error(f"Error cleaning decimal value '{value_str}': {str(e)}")
        return "0.00"


# ============================================================================
# Vendor Ledger Lookup (shared by vendor and expense views)
# ============================================================================

_GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z0-9]{13}$")


def _gstin_pan(gstin):
    """PAN part of a well-formed GSTIN (chars 3-12), else ``None``."""
    value = re.sub(r"\s+", "", str(gstin or "")).upper()
    return value[2:12] if _GSTIN_RE.match(value) else None


def find_tally_vendor_ledger(company_name, organization, vendor_gst=None,
                             similarity_threshold=0.70, use_config_threshold=False,
                             gst_search_scope='parents', fallback_all_ledgers=False,
                             party_parents_field='vendor_parents'):
    """
    Find matching vendor ledger using GST → exact name → similarity.

    Args:
        company_name: Company name from the bill
        organization: Organization instance
        vendor_gst: Optional GST number for priority matching
        similarity_threshold: Minimum similarity score (default 0.70)
        use_config_threshold: If True, read threshold from TallyConfig
        gst_search_scope: 'parents' = search only under vendor parents,
                          'all' = search all org ledgers for GST
        fallback_all_ledgers: If True and no TallyConfig, search all non-root ledgers
        party_parents_field: Which TallyConfig group scopes the search.
                          'vendor_parents' for purchases (Sundry Creditors),
                          'customer_parents' for sales (Sundry Debtors). The
                          two must not share a scope: a customer offered as a
                          vendor posts the sale against the wrong side.
    """
    from ..models import TallyConfig, Ledger

    try:
        if not company_name or not company_name.strip():
            return None

        tally_config = TallyConfig.objects.filter(organization=organization).first()

        # Determine vendor ledger queryset
        if tally_config:
            vendor_parent_ledgers = getattr(tally_config, party_parents_field).all()
            if not vendor_parent_ledgers.exists():
                logger.warning(f"No vendor parent ledgers configured in TallyConfig for org {organization.id}")
                return None
            vendor_ledgers = Ledger.objects.filter(
                organization=organization,
                parent__in=vendor_parent_ledgers
            )
        elif fallback_all_ledgers:
            vendor_ledgers = Ledger.objects.filter(
                organization=organization
            ).exclude(parent__isnull=True)
        else:
            logger.warning(f"No TallyConfig found for organization {organization.id}")
            return None

        # Step 1: GST match — the party's own group first, always.
        #
        # ``gst_search_scope='all'`` used to search every ledger in the
        # organisation straight away. The journal and payment vouchers
        # ask for that, and a GSTIN is unique per registration, so it
        # looked safe. It is not: a firm you both buy from and sell to —
        # ordinary in Indian trade — has that same GSTIN on a ledger
        # under Sundry Debtors *and* one under Sundry Creditors, and
        # whichever the database returned first won. A payment matched
        # to the debtor posts the money on the wrong side of the books.
        #
        # So the scoped set is tried first whatever the scope, and the
        # wide search stays as the fallback it was meant to be: the
        # journal really does reference ledgers outside the vendor
        # group, and that still works.
        if vendor_gst and vendor_gst.strip():
            _gst = vendor_gst.strip()
            gst_match = vendor_ledgers.filter(gst_in__iexact=_gst).first()
            if gst_match is None and gst_search_scope != 'parents':
                gst_match = (
                    Ledger.objects
                    .filter(organization=organization, gst_in__iexact=_gst)
                    .first()
                )
                if gst_match:
                    logger.info(
                        "GST matched outside the %s group: ledger %s",
                        party_parents_field, gst_match.id,
                    )
            if gst_match:
                logger.info("Found party by GST match: ledger %s", gst_match.id)
                return gst_match

        # A name match must not pick a ledger registered under a DIFFERENT
        # business: when both the bill and the ledger carry a valid GSTIN,
        # their PAN part (chars 3-12) must agree. Same PAN / other state
        # (multi-state vendor) is still fine; ledgers without GSTIN too.
        bill_pan = _gstin_pan(vendor_gst)

        def _pan_conflict(ledger):
            ledger_pan = _gstin_pan(ledger.gst_in)
            return bool(bill_pan and ledger_pan and bill_pan != ledger_pan)

        # Step 2: Exact name match
        normalized_name = normalize_company_name(company_name)
        exact = next(
            (l for l in vendor_ledgers.filter(name__iexact=company_name.strip())
             if not _pan_conflict(l)),
            None,
        )
        if exact:
            logger.info(f"Found vendor by exact name: {exact.name}")
            return exact

        # Step 3: Similarity matching
        threshold = similarity_threshold
        if use_config_threshold and tally_config:
            try:
                enhancement = float(tally_config.tally_vendor_match_enhancement or 1.0)
                config_thresh = float(tally_config.tally_vendor_match_threshold or 0.8)
                threshold = config_thresh * enhancement
            except (ValueError, AttributeError):
                pass

        best_match = None
        best_similarity = 0.0

        for ledger in vendor_ledgers:
            if _pan_conflict(ledger):
                continue
            normalized_ledger = normalize_company_name(ledger.name)

            # Normalized exact match
            if normalized_name.lower() == normalized_ledger.lower():
                logger.info(f"Found vendor by normalized match: {ledger.name}")
                return ledger

            similarity = calculate_string_similarity(normalized_name, normalized_ledger)

            # Contains-match boost
            if len(normalized_name) > 10 and len(normalized_ledger) > 10:
                if normalized_name.lower() in normalized_ledger.lower() or normalized_ledger.lower() in normalized_name.lower():
                    similarity = max(similarity, 0.8)

            if similarity > best_similarity:
                best_similarity = similarity
                best_match = ledger

        if best_match and best_similarity >= threshold:
            logger.info(f"Found vendor by similarity ({best_similarity:.2f}): {best_match.name}")
            return best_match

        # Step 4: Contains fallback (if using config threshold — expense style)
        if use_config_threshold:
            contains = next(
                (l for l in vendor_ledgers.filter(name__icontains=company_name)
                 if not _pan_conflict(l)),
                None,
            )
            if contains:
                logger.info(f"Found vendor by contains match: {contains.name}")
                return contains

        logger.info(f"No vendor found for: '{company_name}' (best similarity: {best_similarity:.2f})")
        return None

    except Exception as e:
        logger.error(f"Error in find_tally_vendor_ledger: {str(e)}")
        return None


# ============================================================================
# Tax Ledger Lookup (shared by vendor and expense views)
# ============================================================================

def find_tally_tax_ledger(organization, igst_val, cgst_val, sgst_val, ledger_type='bill'):
    """
    Find appropriate tax ledger based on GST values and TallyConfig.

    Args:
        organization: Organization instance
        igst_val: IGST amount
        cgst_val: CGST amount
        sgst_val: SGST amount
        ledger_type: 'bill' = Duties & Taxes GST ledgers
                     'product' = Purchase Account ledgers (vendor)
                     'expense_coa' = Expense COA ledgers (expense)
    """
    from ..models import TallyConfig, Ledger

    try:
        tally_config = TallyConfig.objects.filter(organization=organization).first()
        if not tally_config:
            logger.warning(f"No TallyConfig found for organization {organization.id}")
            return None

        # --- Product-level: Purchase Accounts (vendor bills) ---
        if ledger_type == 'product':
            parents = tally_config.chart_of_accounts_parents.all()
            if not parents.exists():
                return None
            ledgers = Ledger.objects.filter(organization=organization, parent__in=parents)
            return (
                ledgers.filter(name__icontains='purchase').first()
                or ledgers.filter(name__icontains='expense').first()
                or ledgers.first()
            )

        # --- Expense COA: Indirect Expenses etc. (expense bills) ---
        if ledger_type == 'expense_coa':
            parents = tally_config.chart_of_accounts_expense_parents.all()
            if not parents.exists():
                return None
            ledgers = Ledger.objects.filter(organization=organization, parent__in=parents)
            return (
                ledgers.filter(name__icontains='expense').first()
                or ledgers.filter(name__icontains='indirect').first()
                or ledgers.first()
            )

        # --- Bill-level GST ledgers (Duties & Taxes) ---
        gst_config = {
            'igst': (igst_val, cgst_val, sgst_val, tally_config.igst_parents, 'igst'),
            'cgst': (cgst_val, igst_val, sgst_val, tally_config.cgst_parents, 'cgst'),
            'sgst': (sgst_val, igst_val, cgst_val, tally_config.sgst_parents, 'sgst'),
        }

        for _key, (primary, other1, other2, parent_rel, keyword) in gst_config.items():
            if primary > 0 and other1 == 0 and other2 == 0:
                parent_ledgers = parent_rel.all()
                if parent_ledgers.exists():
                    tax_ledgers = Ledger.objects.filter(
                        organization=organization, parent__in=parent_ledgers
                    )
                    match = tax_ledgers.filter(name__icontains=keyword).first() or tax_ledgers.first()
                    if match:
                        logger.info(f"Found {keyword.upper()} tax ledger: {match.name}")
                        return match

        # Fallback: any available tax parent
        all_parents = set()
        for rel in [tally_config.igst_parents, tally_config.cgst_parents, tally_config.sgst_parents]:
            all_parents.update(rel.all())

        if all_parents:
            fallback = Ledger.objects.filter(
                organization=organization, parent__in=list(all_parents)
            ).first()
            if fallback:
                logger.info(f"Fallback tax ledger: {fallback.name}")
                return fallback

        logger.warning(f"No tax ledger found for IGST:{igst_val}, CGST:{cgst_val}, SGST:{sgst_val}")
        return None

    except Exception as e:
        logger.error(f"Error finding tax ledger: {str(e)}")
        return None


# ============================================================================
# Find-or-Create Vendor Ledger (shared by vendor and expense verify views)
# ============================================================================

def find_or_create_tally_vendor_ledger(vendor_name, vendor_data, organization,
                                       default_parent_name="Sundry Creditors",
                                       create_default_parent=True,
                                       party_parents_field='vendor_parents'):
    """
    Find existing vendor ledger or create a new one using TallyConfig.

    Args:
        vendor_name: Name to search/create
        vendor_data: Dict with optional master_id, gst_in, company
        organization: Organization instance
        default_parent_name: Fallback parent ledger name
        create_default_parent: If True, create default parent when TallyConfig absent
        party_parents_field: Which TallyConfig group a newly created ledger is
                          filed under — see find_tally_vendor_ledger.
    """
    from ..models import TallyConfig, Ledger, ParentLedger

    try:
        # Try exact match first
        vendor = Ledger.objects.filter(
            name__iexact=vendor_name.strip(),
            organization=organization
        ).first()

        if vendor:
            # Update details if provided
            updated_fields = []
            for field in ('master_id', 'gst_in', 'company'):
                val = vendor_data.get(field) if vendor_data else None
                if val and val != "No Ledger":
                    setattr(vendor, field, val)
                    updated_fields.append(field)
            if updated_fields:
                vendor.save(update_fields=updated_fields)
            return vendor

        # Determine parent ledger
        tally_config = TallyConfig.objects.filter(organization=organization).first()
        parent_ledger = None

        if tally_config:
            vendor_parents = getattr(tally_config, party_parents_field).all()
            if vendor_parents.exists():
                parent_ledger = vendor_parents.first()

        if not parent_ledger:
            if not create_default_parent:
                logger.warning(f"No vendor parent configured and create_default_parent=False")
                return None
            parent_ledger, _ = ParentLedger.objects.get_or_create(
                parent=default_parent_name,
                organization=organization
            )

        # Create new vendor
        vendor = Ledger.objects.create(
            name=vendor_name.strip(),
            parent=parent_ledger,
            master_id=vendor_data.get('master_id') if vendor_data and vendor_data.get('master_id') != "No Ledger" else None,
            gst_in=vendor_data.get('gst_in') if vendor_data and vendor_data.get('gst_in') != "No Ledger" else None,
            company=vendor_data.get('company') if vendor_data and vendor_data.get('company') != "No Ledger" else None,
            organization=organization
        )
        return vendor

    except Exception as e:
        logger.error(f"Error finding/creating vendor ledger: {str(e)}")
        return None


# ============================================================================
# Find-or-Create Tax Ledger (shared by vendor and expense verify views)
# ============================================================================

def find_or_create_tally_tax_ledger(ledger_name, tax_type, organization):
    """
    Find existing tax ledger or create a new one using TallyConfig.

    Args:
        ledger_name: Name to search/create
        tax_type: 'IGST', 'CGST', 'SGST', or other
        organization: Organization instance
    """
    from ..models import TallyConfig, Ledger, ParentLedger

    try:
        tax_ledger = Ledger.objects.filter(
            name__iexact=ledger_name.strip(),
            organization=organization
        ).first()
        if tax_ledger:
            return tax_ledger

        tally_config = TallyConfig.objects.filter(organization=organization).first()
        parent_ledger = None

        if tally_config:
            # Normalise the tax_type key so both upper- and lower-case
            # callers (`ROUND_OFF`, `round_off`, `COA`, `chart_of_accounts`)
            # resolve to the same slot. Previously any type outside
            # {IGST,CGST,SGST} silently landed under "Duties & Taxes".
            key = (tax_type or '').strip().upper()
            type_map = {
                'IGST': tally_config.igst_parents,
                'CGST': tally_config.cgst_parents,
                'SGST': tally_config.sgst_parents,
                'TDS': tally_config.tds_parents,
                'ROUND_OFF': tally_config.round_off_parents,
                'ROUNDOFF': tally_config.round_off_parents,
                'COA': tally_config.chart_of_accounts_parents,
                'CHART_OF_ACCOUNTS': tally_config.chart_of_accounts_parents,
                'PURCHASE': tally_config.chart_of_accounts_parents,
                # Sales lines belong to the revenue accounts, not the
                # purchase ones; without this a sales ledger created on
                # the fly lands under Purchase Accounts.
                'SALES': tally_config.sales_coa_parents,
                # Adjustments now DO have dedicated slots —
                # discount/cess/freight_parents arrived in migration 0038,
                # the config endpoint saves them and the setup page shows
                # them. Nothing read them, so an operator who set
                # "Discount ledgers -> Indirect Expenses" watched a
                # quick-created discount ledger land under Purchase
                # Accounts anyway: configuration that silently did
                # nothing.
                #
                # The dedicated group is tried first and the
                # chart-of-accounts group is kept behind it, so an
                # organisation that never configured these keeps exactly
                # the placement it has today.
                'DISCOUNT': (tally_config.discount_parents,
                             tally_config.chart_of_accounts_parents),
                'CESS': (tally_config.cess_parents,
                         tally_config.chart_of_accounts_parents),
                'FREIGHT': (tally_config.freight_parents,
                            tally_config.chart_of_accounts_parents),
                'OTHER': tally_config.chart_of_accounts_parents,
                'PRODUCT TAX': tally_config.chart_of_accounts_parents,
            }
            parent_rel = type_map.get(key)
            # A tuple means "this group, else that one".
            for _rel in (parent_rel if isinstance(parent_rel, tuple)
                         else (parent_rel,)):
                if _rel is None:
                    continue
                parents = _rel.all()
                if parents.exists():
                    parent_ledger = parents.first()
                    break
            if parent_ledger is None:
                # Last-resort fallback: union of GST parents so an unknown
                # tax_type still lands in a Duties & Taxes group rather
                # than orphaned under a new default.
                combined = (tally_config.igst_parents.all() |
                            tally_config.cgst_parents.all() |
                            tally_config.sgst_parents.all())
                if combined.exists():
                    parent_ledger = combined.first()

        if not parent_ledger:
            parent_ledger, _ = ParentLedger.objects.get_or_create(
                parent="Duties & Taxes",
                organization=organization
            )

        tax_ledger = Ledger.objects.create(
            name=ledger_name.strip(),
            parent=parent_ledger,
            organization=organization
        )
        logger.info(f"Created tax ledger: {tax_ledger.name} under {parent_ledger.parent}")
        return tax_ledger

    except Exception as e:
        logger.error(f"Error finding/creating tax ledger: {str(e)}")
        return None


# ============================================================================
# Find-or-Create Chart of Accounts Ledger (expense-specific)
# ============================================================================

def find_coa_ledger(coa_name, organization):
    """Find existing chart of accounts ledger by exact name match."""
    from ..models import Ledger

    try:
        return Ledger.objects.filter(
            name__iexact=coa_name.strip(),
            organization=organization
        ).first()
    except Exception as e:
        logger.error(f"Error finding COA ledger: {str(e)}")
        return None



def _quantity_decimal(value):
    """A quantity as stored: three decimal places, never float noise."""
    from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

    try:
        return Decimal(str(value)).quantize(
            Decimal('0.001'), rounding=ROUND_HALF_UP
        )
    except (InvalidOperation, ValueError, TypeError):
        return Decimal('0')


def read_line_amounts(item, context=""):
    """Read one extracted line's price, quantity and amount.

    The quantity columns were ``PositiveIntegerField`` until migration
    0058, so "1.5 kg" — an ordinary line on an Indian invoice — could
    not be stored and the figure that reached Tally was the truncated
    one. They are ``DecimalField(decimal_places=3)`` now and the
    quantity is kept as read.

    Two things followed from the old column and are kept fixed here:

    * The fresh-analysis path multiplied the price by the *float*
      quantity and kept the money right; the re-analysis path
      multiplied by the truncated int, so re-analysing a bill turned a
      2.5 x 100 line from 250 into 200 with nothing on screen saying the
      total had moved. Both paths are this function now.
    * A negative quantity tripped the column's check constraint and took
      the whole analysis down. It is clamped, loudly.

    Returns ``(price, quantity, amount)`` — price and amount as floats
    for the caller to round, quantity as a ``Decimal``.
    """
    from decimal import Decimal

    price = safe_float_convert(item.get('price', 0))
    quantity_float = safe_float_convert(item.get('quantity', 0))
    quantity = _quantity_decimal(quantity_float)

    # The column carries a Postgres check constraint, so a negative
    # quantity does not store badly — it raises IntegrityError and takes
    # the whole analysis down with it. A line reading "LESS: RETURN 2
    # PCS" is ordinary on an invoice, so this is reachable from a real
    # document, and losing the bill is worse than losing the sign.
    #
    # The quantity is clamped and the amount keeps whatever sign the
    # arithmetic gave it, because the money is the part that has to stay
    # right. The warning is what makes the dropped sign findable.
    if quantity < 0:
        logger.warning(
            "Negative quantity %s on item %r%s — stored as 0. The line "
            "amount keeps its sign; the quantity does not.",
            quantity, item.get('description', 'unknown'),
            f" ({context})" if context else "",
        )
        quantity = Decimal('0')

    return price, quantity, price * quantity_float

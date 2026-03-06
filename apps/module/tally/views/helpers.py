# apps/module/tally/views/helpers.py
"""
Shared helpers used across tally views.
Kept here to avoid circular imports between view modules.
"""
import logging

from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from apps.common.permissions import OrganizationAPIKeyOrBearerToken  # noqa: F401 – re-exported
from apps.common.utils import normalize_company_name, calculate_string_similarity

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

def find_tally_vendor_ledger(company_name, organization, vendor_gst=None,
                             similarity_threshold=0.70, use_config_threshold=False,
                             gst_search_scope='parents', fallback_all_ledgers=False):
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
    """
    from ..models import TallyConfig, Ledger

    try:
        if not company_name or not company_name.strip():
            return None

        tally_config = TallyConfig.objects.filter(organization=organization).first()

        # Determine vendor ledger queryset
        if tally_config:
            vendor_parent_ledgers = tally_config.vendor_parents.all()
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

        # Step 1: GST match
        if vendor_gst and vendor_gst.strip():
            gst_qs = vendor_ledgers if gst_search_scope == 'parents' else Ledger.objects.filter(organization=organization)
            gst_match = gst_qs.filter(gst_in__iexact=vendor_gst.strip()).first()
            if gst_match:
                logger.info(f"Found vendor by GST match: {gst_match.name} (GST: {gst_match.gst_in})")
                return gst_match

        # Step 2: Exact name match
        normalized_name = normalize_company_name(company_name)
        exact = vendor_ledgers.filter(name__iexact=company_name.strip()).first()
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
            contains = vendor_ledgers.filter(name__icontains=company_name).first()
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
                                       create_default_parent=True):
    """
    Find existing vendor ledger or create a new one using TallyConfig.

    Args:
        vendor_name: Name to search/create
        vendor_data: Dict with optional master_id, gst_in, company
        organization: Organization instance
        default_parent_name: Fallback parent ledger name
        create_default_parent: If True, create default parent when TallyConfig absent
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
            vendor_parents = tally_config.vendor_parents.all()
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
            type_map = {
                'IGST': tally_config.igst_parents,
                'CGST': tally_config.cgst_parents,
                'SGST': tally_config.sgst_parents,
            }
            parent_rel = type_map.get(tax_type)
            if parent_rel:
                parents = parent_rel.all()
                if parents.exists():
                    parent_ledger = parents.first()
            else:
                # Fallback: union of all tax parents
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

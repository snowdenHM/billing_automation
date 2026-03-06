"""
Shared bill-creation utilities for Zoho expense, journal, and vendor views.

Eliminates duplication across create_*_zoho_objects_from_analysis functions:
- ``flatten_analyzed_schema``  – normalise OpenAI response format
- ``parse_analysis_dates``     – parse bill/due dates (single or multi-format)
- ``lookup_vendor_from_analysis`` – enhanced GST-aware vendor lookup
- ``create_zoho_objects_from_analysis`` – shared core for expense & journal
"""

import logging
from datetime import datetime
from decimal import Decimal

from django.db.models.functions import Lower

from ..models import ZohoVendor
from .helpers import (
    find_zoho_vendor_ledger_enhanced,
    find_appropriate_zoho_coa_ledger,
    find_appropriate_zoho_tax_ledger,
    safe_numeric_string,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

_DATE_FORMATS = ['%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%Y/%m/%d']


def flatten_analyzed_schema(analyzed_data):
    """Normalise OpenAI analysis output, handling both nested-schema and flat formats."""
    if "properties" not in analyzed_data:
        return analyzed_data

    props = analyzed_data["properties"]
    return {
        "invoiceNumber": props["invoiceNumber"]["const"],
        "dateIssued": props["dateIssued"]["const"],
        "dueDate": props["dueDate"]["const"],
        "from": props["from"]["properties"],
        "to": props["to"]["properties"],
        "items": [
            {
                "description": item["description"]["const"],
                "quantity": item["quantity"]["const"],
                "price": item["price"]["const"],
            }
            for item in props["items"]["items"]
        ],
        "total": props["total"]["const"],
        "igst": props["igst"]["const"],
        "cgst": props["cgst"]["const"],
        "sgst": props["sgst"]["const"],
    }


def parse_analysis_dates(relevant_data, *, multi_format=False):
    """
    Parse bill_date and due_date from flattened analysis data.

    Args:
        relevant_data: Dict returned by ``flatten_analyzed_schema``.
        multi_format: If *True*, try multiple date formats (vendor bills).
                      Otherwise only ``%Y-%m-%d``.

    Returns:
        ``(bill_date, due_date)`` tuple of ``date | None``.
    """
    formats = _DATE_FORMATS if multi_format else ['%Y-%m-%d']

    def _try_parse(value, label):
        if not value:
            return None
        for fmt in formats:
            try:
                return datetime.strptime(value, fmt).date()
            except (ValueError, TypeError):
                continue
        logger.warning(f"Could not parse {label}: {value}")
        return None

    bill_date = _try_parse(relevant_data.get('dateIssued', ''), 'bill date')
    due_date = _try_parse(relevant_data.get('dueDate', ''), 'due date')
    return bill_date, due_date


def lookup_vendor_from_analysis(relevant_data, organization, *, use_fallback=False):
    """
    Look up a vendor from analysis data using enhanced GST-aware matching.

    Args:
        relevant_data: Dict returned by ``flatten_analyzed_schema``.
        organization: ``Organization`` instance.
        use_fallback: If *True*, fall back to basic case-insensitive name
                      search when enhanced lookup raises (vendor bills).

    Returns:
        ``(vendor, company_name, vendor_gst)`` tuple.
    """
    vendor = None
    company_name = relevant_data.get('from', {}).get('name', '').strip()
    vendor_gst = relevant_data.get('from', {}).get('gst_number', '').strip()

    if company_name:
        if use_fallback:
            try:
                vendor = find_zoho_vendor_ledger_enhanced(company_name, organization, vendor_gst)
                logger.info(f"✅ Found vendor using enhanced lookup - {company_name}: {vendor}")
            except Exception as e:
                logger.warning(f"Enhanced vendor lookup failed for {company_name}: {str(e)}")
                vendor = ZohoVendor.objects.annotate(
                    lower_name=Lower('companyName')
                ).filter(lower_name=company_name.lower()).first()
                logger.info(f"Fallback vendor lookup - {company_name}: {vendor}")
        else:
            vendor = find_zoho_vendor_ledger_enhanced(company_name, organization, vendor_gst)
            if vendor:
                logger.info(f"✅ Matched vendor: {vendor.companyName}")
            else:
                logger.warning(f"⚠️ No vendor match found for: {company_name}")

    return vendor, company_name, vendor_gst


# ---------------------------------------------------------------------------
# Shared bill + product + consolidated-product creation (expense & journal)
# ---------------------------------------------------------------------------

def create_zoho_objects_from_analysis(
    bill,
    analyzed_data,
    organization,
    *,
    bill_model,
    product_model,
    consolidated_model,
    ledger_type,
    bill_type_label,
    assign_taxes=False,
):
    """
    Create ZohoBill, ZohoProduct, and ZohoConsolidatedProduct from analysis.

    This is the shared core used by both expense and journal views.  Vendor
    bills have additional logic (ownership validation, richer product fields,
    duplicate detection) and therefore keep their own implementation but reuse
    the schema/date/vendor helpers above.

    Args:
        bill: Upload model instance (``ExpenseBill`` / ``JournalBill``).
        analyzed_data: Raw OpenAI analysis dict.
        organization: ``Organization`` instance.
        bill_model: e.g. ``ExpenseZohoBill``.
        product_model: e.g. ``ExpenseZohoProduct``.
        consolidated_model: e.g. ``ExpenseZohoConsolidatedProduct``.
        ledger_type: ``'expense'`` or ``'journal'`` (passed to COA lookup).
        bill_type_label: Human label for log messages.
        assign_taxes: Auto-assign taxes on each product (expense only).

    Returns:
        The created / updated ``ZohoBill`` instance.
    """
    logger.info(f"Creating {bill_type_label} Zoho objects for bill {bill.id}")

    relevant_data = flatten_analyzed_schema(analyzed_data)
    vendor, company_name, vendor_gst = lookup_vendor_from_analysis(relevant_data, organization)
    bill_date, due_date = parse_analysis_dates(relevant_data)

    try:
        zoho_bill, created = bill_model.objects.get_or_create(
            selectBill=bill,
            organization=organization,
            defaults={
                'vendor': vendor,
                'bill_no': relevant_data.get('invoiceNumber', ''),
                'bill_date': bill_date,
                'due_date': due_date,
                'total': safe_numeric_string(relevant_data.get('total')),
                'igst': safe_numeric_string(relevant_data.get('igst')),
                'cgst': safe_numeric_string(relevant_data.get('cgst')),
                'sgst': safe_numeric_string(relevant_data.get('sgst')),
                'note': (
                    f"✅ Auto-filled from analysis | Vendor: "
                    f"{company_name or 'Unknown'} | Analysed via Billmunshi"
                )[:95] + "...",
            },
        )

        if created:
            logger.info(f"Created new {bill_type_label} bill: {zoho_bill.id}")
        else:
            logger.info(f"Found existing {bill_type_label} bill: {zoho_bill.id}")
            zoho_bill.vendor = vendor
            zoho_bill.bill_no = relevant_data.get('invoiceNumber', zoho_bill.bill_no)
            zoho_bill.bill_date = bill_date or zoho_bill.bill_date
            zoho_bill.due_date = due_date or zoho_bill.due_date
            zoho_bill.total = safe_numeric_string(relevant_data.get('total'), zoho_bill.total)
            zoho_bill.igst = safe_numeric_string(relevant_data.get('igst'), zoho_bill.igst)
            zoho_bill.cgst = safe_numeric_string(relevant_data.get('cgst'), zoho_bill.cgst)
            zoho_bill.sgst = safe_numeric_string(relevant_data.get('sgst'), zoho_bill.sgst)
            zoho_bill.note = (
                f"✅ Updated from re-analysis | Vendor: {company_name or 'Unknown'}"
            )[:95] + "..."
            zoho_bill.save()
            logger.info(f"Updated existing {bill_type_label} bill: {zoho_bill.id}")

        # Delete existing products and recreate
        existing_count = zoho_bill.products.count()
        if existing_count > 0:
            zoho_bill.products.all().delete()
            logger.info(f"Deleted {existing_count} existing products")

        # Create product line items
        items = relevant_data.get('items', [])
        logger.info(f"Creating {len(items)} product line items")

        created_products = []
        for idx, item in enumerate(items):
            try:
                amount = item.get('price', 0) * item.get('quantity', 1)
                item_description = item.get('description', f'Item {idx + 1}')

                chart_of_accounts = find_appropriate_zoho_coa_ledger(
                    organization, item_description, ledger_type=ledger_type,
                )

                product_kwargs = dict(
                    zohoBill=zoho_bill,
                    organization=organization,
                    item_details=item_description[:2000],
                    amount=safe_numeric_string(amount),
                    chart_of_accounts=chart_of_accounts,
                )

                # Auto-assign taxes for expense bills
                taxes = None
                if assign_taxes:
                    if relevant_data.get('igst', 0) > 0:
                        taxes = find_appropriate_zoho_tax_ledger(
                            organization, 'igst', relevant_data.get('igst', 0),
                        )
                    elif relevant_data.get('cgst', 0) > 0 or relevant_data.get('sgst', 0) > 0:
                        taxes = find_appropriate_zoho_tax_ledger(
                            organization, 'cgst', relevant_data.get('cgst', 0),
                        )
                    product_kwargs['taxes'] = taxes

                product = product_model.objects.create(**product_kwargs)
                created_products.append(product)

                coa_info = (
                    f" | CoA: {chart_of_accounts.accountName}"
                    if chart_of_accounts else " | CoA: Not assigned"
                )
                tax_info = f" | Tax: {taxes.taxName}" if taxes else ""
                logger.info(
                    f"✅ Created product {idx + 1}: "
                    f"{product.item_details[:50]}{coa_info}{tax_info} - Amount: {product.amount}"
                )
            except Exception as e:
                logger.error(f"Error creating product {idx + 1}: {str(e)}")
                continue

        logger.info(f"Successfully created {len(created_products)} products for bill {zoho_bill.id}")

        # Auto-create consolidated product for multi-item bills
        if len(created_products) > 1:
            try:
                consolidated_model.objects.filter(zohoBill=zoho_bill).delete()

                total_amount = sum(Decimal(str(p.amount or 0)) for p in created_products)
                items_count = len(created_products)

                detail_lines = [
                    f'• {p.item_details} (Amount: ₹{p.amount})' for p in created_products
                ]
                consolidated_details = (
                    f'Consolidated {items_count} {bill_type_label} entries:\n'
                    + '\n'.join(detail_lines)
                )

                consolidated_model.objects.create(
                    zohoBill=zoho_bill,
                    organization=organization,
                    consolidated_item_details=consolidated_details,
                    consolidated_amount=total_amount,
                    original_entries_count=items_count,
                    consolidation_notes=(
                        f'Auto-created during analysis for {items_count} {bill_type_label} entries'
                    ),
                )
                logger.info(
                    f"✅ Auto-created consolidated {bill_type_label} product for bill "
                    f"{zoho_bill.id} with {items_count} entries (₹{total_amount})"
                )
            except Exception as e:
                logger.error(
                    f"❌ Error creating consolidated {bill_type_label} product "
                    f"for bill {zoho_bill.id}: {str(e)}"
                )
        else:
            logger.info(
                f"ℹ️ Skipping consolidated {bill_type_label} product creation - "
                f"bill has only {len(created_products)} item(s)"
            )

        return zoho_bill

    except Exception as e:
        logger.error(f"Error creating Zoho objects for bill {bill.id}: {str(e)}")
        raise

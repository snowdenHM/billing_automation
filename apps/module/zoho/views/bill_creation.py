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
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models.functions import Lower

from ..models import ZohoVendor
from .helpers import (
    find_zoho_customer_ledger_enhanced,
    find_zoho_vendor_ledger_enhanced,
    find_appropriate_zoho_coa_ledger,
    find_appropriate_zoho_tax_ledger,
    safe_numeric_string,
)

logger = logging.getLogger(__name__)


def _dec(value, default="0"):
    """Coerce an analysis value to Decimal. Never multiply raw JSON values:
    a model returning ``"price": "12"`` made ``"12" * 3 == "121212"``."""
    try:
        return Decimal(str(value if value not in (None, "") else default))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def _line_amount(item):
    qty = _dec(item.get("quantity", 1), "1")
    return _dec(item.get("price", 0)) * qty

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


def lookup_vendor_from_analysis(relevant_data, organization, *, use_fallback=False, source='from'):
    """
    Look up a vendor from analysis data using enhanced GST-aware matching.

    Args:
        relevant_data: Dict returned by ``flatten_analyzed_schema``.
        organization: ``Organization`` instance.
        use_fallback: If *True*, fall back to basic case-insensitive name
                      search when enhanced lookup raises (vendor bills).
        source: Which half of the document the party is read from —
                ``'from'`` (the issuer) on a purchase bill, ``'to'`` (the
                recipient) when the organisation itself issued the document.

    Returns:
        ``(vendor, company_name, vendor_gst)`` tuple.
    """
    vendor = None
    party = relevant_data.get(source) or {}
    company_name = (party.get('name') or '').strip()
    vendor_gst = (party.get('gst_number') or '').strip()

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


def lookup_party_from_analysis(relevant_data, organization, *, source='from', lookup_fn=None):
    """Side-agnostic party lookup.

    ``lookup_vendor_from_analysis`` stays as the purchase-side entry point
    (and its ``use_fallback`` path) because vendor bills call it directly;
    this is the same thing with the contact model and the document half
    both parameterised.
    """
    lookup_fn = lookup_fn or find_zoho_vendor_ledger_enhanced
    party_data = relevant_data.get(source) or {}
    company_name = (party_data.get('name') or '').strip()
    gst = (party_data.get('gst_number') or '').strip()

    party = lookup_fn(company_name, organization, gst) if company_name else None
    if company_name and not party:
        logger.warning("No contact match found for: %s (source=%s)", company_name, source)
    return party, company_name, gst


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
    party_field='vendor',
    party_source='from',
    party_lookup_fn=None,
    include_rate_quantity=False,
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
        party_field: Name of the FK on ``bill_model`` holding the party —
            ``'vendor'`` on the purchase side, ``'customer'`` on the sales side.
        party_source: ``'from'`` or ``'to'`` — which half of the document the
            party is read from. A sales invoice is a purchase bill read from
            the other end, so the organisation is the issuer and the party is
            the recipient.
        party_lookup_fn: Contact lookup; defaults to the vendor one.
        include_rate_quantity: Also populate ``item_name``/``rate``/``quantity``
            on each product. Zoho's ``invoices`` line items price per unit, so
            the sales side needs them; the expense/journal feeds post a flat
            amount and have no such columns.

    Returns:
        The created / updated ``ZohoBill`` instance.
    """
    logger.info(f"Creating {bill_type_label} Zoho objects for bill {bill.id}")

    relevant_data = flatten_analyzed_schema(analyzed_data)
    party, company_name, _party_gst = lookup_party_from_analysis(
        relevant_data, organization,
        source=party_source,
        lookup_fn=party_lookup_fn or find_zoho_vendor_ledger_enhanced,
    )
    bill_date, due_date = parse_analysis_dates(relevant_data)

    try:
        zoho_bill, created = bill_model.objects.get_or_create(
            selectBill=bill,
            organization=organization,
            defaults={
                party_field: party,
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
            setattr(zoho_bill, party_field, party)
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

        # Rebuild is delete-then-recreate: without a transaction a failure
        # partway through left the bill with fewer lines than its total.
        created_products = []
        failed_products = []
        with transaction.atomic():
            # Delete existing products and recreate
            existing_count = zoho_bill.products.count()
            if existing_count > 0:
                zoho_bill.products.all().delete()
                logger.info(f"Deleted {existing_count} existing products")

            # Create product line items
            items = relevant_data.get('items', [])
            logger.info(f"Creating {len(items)} product line items")

            for idx, item in enumerate(items):
                try:
                    amount = _line_amount(item)
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

                    if include_rate_quantity:
                        product_kwargs['item_name'] = item_description[:1000]
                        product_kwargs['rate'] = safe_numeric_string(_dec(item.get('price', 0)))
                        product_kwargs['quantity'] = safe_numeric_string(_dec(item.get('quantity', 1), '1'))

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
                    failed_products.append(f"line {idx + 1}: {e}")

            if failed_products:
                # Roll back: a bill with some lines missing but the full
                # invoice total is silent money drift.
                raise RuntimeError(
                    "Could not rebuild all line items for Zoho bill "
                    f"{zoho_bill.id}: " + "; ".join(failed_products[:5])
                )

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


# ---------------------------------------------------------------------------
# Payment-object creation (receipt & payment vouchers)
# ---------------------------------------------------------------------------

def create_zoho_payment_objects_from_analysis(
    bill,
    analyzed_data,
    organization,
    *,
    analysed_model,
    party_field,
    party_source,
    party_lookup_fn,
    label,
):
    """Create the analysed header for a receipt or payment voucher.

    Zoho's ``customerpayments`` / ``vendorpayments`` are payment objects:
    a party, an amount, a date, a mode and a bank/cash account. There is
    no line-item grid, so unlike
    :func:`create_zoho_objects_from_analysis` this writes one row and
    stops — the ``expenses`` array the analysis prompt returns describes
    *what the money was for*, which Zoho carries as free text, not as
    postable lines.

    The bank/cash account is deliberately left unset: which account the
    money moved through is not on the document, and guessing it would put
    a plausible-looking wrong account in front of an operator who is about
    to click Verify. Sync refuses without it.
    """
    logger.info("Creating %s Zoho objects for bill %s", label, bill.id)

    relevant_data = flatten_analyzed_schema(analyzed_data)
    party, company_name, _gst = lookup_party_from_analysis(
        relevant_data, organization, source=party_source, lookup_fn=party_lookup_fn,
    )
    bill_date, _due_date = parse_analysis_dates(relevant_data)

    amount = _dec(relevant_data.get('total', 0))
    tds = _dec(relevant_data.get('tds', 0))
    note = f"Auto-filled from analysis | Party: {company_name or 'Unknown'}"[:255]

    defaults = {
        party_field: party,
        'bill_no': (relevant_data.get('invoiceNumber') or '')[:50],
        'bill_date': bill_date,
        'amount': amount,
        'tds': tds,
        'note': note,
    }

    analysed, created = analysed_model.objects.get_or_create(
        selectBill=bill, organization=organization, defaults=defaults,
    )

    if not created:
        # Re-analysis refreshes what the document says, but must not erase
        # work. Two things are kept: fields this function never writes (the
        # bank/cash account, the mode, the documents to settle), and any
        # field the new pass came back empty on. A re-run over a page the
        # model read poorly would otherwise replace the vendor the operator
        # picked with None, and ``note`` is the only field worth stamping
        # unconditionally because it describes the run that just happened.
        for field, value in defaults.items():
            if field == 'note' or value not in (None, '', 0, Decimal('0')):
                setattr(analysed, field, value)
        analysed.save()

    logger.info(
        "%s analysed header %s for bill %s (party=%s, amount=%s)",
        "Created" if created else "Updated", analysed.id, bill.id, company_name, amount,
    )
    return analysed

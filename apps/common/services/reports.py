"""
Shared XLSX report generator for bill exports.

Produces a workbook matching the report format the operations team uses
in Google Sheets — one row per line item, with vendor/invoice fields
repeated so each row is self-contained (importable back into any
spreadsheet tool without lookups).

Columns (in order):
    Bill Id, Vendor Name, GSTIN, Bill No, Bill Date, Item Name,
    Item description, Purchase Ledger, Price, Qty, Amount, GST %,
    IGST, IGST Ledger, CGST, CGST Ledger, SGST, SGST Ledger,
    Total GST, Cess, Discount, Freight/Delivery, Round Off, Total

Bills without line items (journal / payment vouchers with no per-item
breakdown) render as a single row with item-level cells left blank.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO

from django.http import HttpResponse

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

logger = logging.getLogger(__name__)


REPORT_COLUMNS = [
    "Bill Id",
    "Vendor Name",
    "GSTIN",
    "Bill No",
    "Bill Date",
    "Item Name",
    "Item description",
    "Purchase Ledger",
    "Price",
    "Qty",
    "Amount",
    "GST %",
    "IGST",
    "IGST Ledger",
    "CGST",
    "CGST Ledger",
    "SGST",
    "SGST Ledger",
    "Total GST",
    "Cess",
    "Discount",
    "Freight/Delivery",
    "Round Off",
    "Total",
]


def _fmt_date(value):
    if value is None:
        return ""
    if isinstance(value, (date, datetime)):
        return value.strftime("%d-%m-%Y")
    return str(value)


def _num(value):
    """Coerce to a plain float for openpyxl; ``None``/blank → ""."""
    if value in (None, ""):
        return ""
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)


def build_report_workbook(rows: list[dict], sheet_title: str = "Bills"):
    """Build an in-memory ``openpyxl`` workbook from ``rows``.

    ``rows`` is a flat list of dicts with keys matching ``REPORT_COLUMNS``
    (case-insensitive lookup by index — the caller assembles rows in the
    same order they appear in ``REPORT_COLUMNS``).
    """
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_title[:31]  # Excel sheet-name limit

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1F2937")
    header_align = Alignment(horizontal="left", vertical="center", wrap_text=True)

    ws.append(REPORT_COLUMNS)
    for col_idx, _ in enumerate(REPORT_COLUMNS, start=1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align

    for row_dict in rows:
        ws.append([row_dict.get(col, "") for col in REPORT_COLUMNS])

    # Reasonable default column widths — Vendor Name and Item Name get more room.
    widths = {
        "Bill Id": 24, "Vendor Name": 32, "GSTIN": 18, "Bill No": 14,
        "Bill Date": 12, "Item Name": 32, "Item description": 40,
        "Purchase Ledger": 24, "Price": 10, "Qty": 8, "Amount": 12,
        "GST %": 8, "IGST": 10, "IGST Ledger": 22, "CGST": 10,
        "CGST Ledger": 22, "SGST": 10, "SGST Ledger": 22, "Total GST": 10,
        "Cess": 8, "Discount": 10, "Freight/Delivery": 14, "Round Off": 10,
        "Total": 12,
    }
    for idx, col in enumerate(REPORT_COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = widths.get(col, 14)

    ws.freeze_panes = "A2"
    return wb


def workbook_to_response(wb, filename: str) -> HttpResponse:
    """Serialize a workbook to an HTTP ``attachment`` response."""
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    response = HttpResponse(
        buf.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


# ---------------------------------------------------------------------------
# Row builders — one per bill shape (vendor-style vs expense-style)
# ---------------------------------------------------------------------------

def _ledger_name(obj):
    """Best-effort name resolver for a FK-related Ledger instance."""
    if obj is None:
        return ""
    return getattr(obj, "name", None) or str(obj)


def rows_from_vendor_analyzed(bill, analyzed_bill):
    """Return report rows for a Purchase-Voucher-style analyzed bill.

    ``analyzed_bill`` must expose the standard Tally-vendor / Zoho-vendor
    shape: ``.vendor.name``, ``.vendor.gst_in`` (optional), ``.bill_no``,
    ``.bill_date``, ``.total``, ``.igst``, ``.cgst``, ``.sgst``, and a
    ``.products.all()`` iterable whose entries carry per-item fields.

    Falls back to bill-level totals when there are no products.
    """
    vendor = getattr(analyzed_bill, "vendor", None)
    vendor_name = _ledger_name(vendor) if vendor else (
        getattr(analyzed_bill, "vendor_name", "") or ""
    )
    gstin = ""
    for attr in ("gst_in", "gstin", "gst_number", "vendor_gstin"):
        val = getattr(vendor, attr, None) if vendor else None
        if val:
            gstin = val
            break

    bill_id = (
        getattr(bill, "bill_munshi_name", None)
        or getattr(bill, "billmunshiName", None)
        or str(getattr(bill, "id", ""))
    )
    bill_no = getattr(analyzed_bill, "bill_no", "") or ""
    bill_date = _fmt_date(getattr(analyzed_bill, "bill_date", None))

    common = {
        "Bill Id": bill_id,
        "Vendor Name": vendor_name,
        "GSTIN": gstin,
        "Bill No": bill_no,
        "Bill Date": bill_date,
    }

    igst_amt = _num(getattr(analyzed_bill, "igst", 0))
    cgst_amt = _num(getattr(analyzed_bill, "cgst", 0))
    sgst_amt = _num(getattr(analyzed_bill, "sgst", 0))
    igst_ledger = _ledger_name(getattr(analyzed_bill, "igst_taxes", None))
    cgst_ledger = _ledger_name(getattr(analyzed_bill, "cgst_taxes", None))
    sgst_ledger = _ledger_name(getattr(analyzed_bill, "sgst_taxes", None))

    def _sum(*vals):
        total = 0.0
        for v in vals:
            if isinstance(v, (int, float)):
                total += v
        return total

    total_gst = _sum(igst_amt, cgst_amt, sgst_amt)
    round_off = _num(getattr(analyzed_bill, "round_off", 0))
    grand_total = _num(getattr(analyzed_bill, "total", 0))
    other_adj = _num(getattr(analyzed_bill, "other_adjustment", 0))

    try:
        products = list(analyzed_bill.products.all())
    except Exception:
        products = []

    rows = []
    if not products:
        rows.append({
            **common,
            "Item Name": "",
            "Item description": "",
            "Purchase Ledger": "",
            "Price": "",
            "Qty": "",
            "Amount": grand_total,
            "GST %": "",
            "IGST": igst_amt or "",
            "IGST Ledger": igst_ledger,
            "CGST": cgst_amt or "",
            "CGST Ledger": cgst_ledger,
            "SGST": sgst_amt or "",
            "SGST Ledger": sgst_ledger,
            "Total GST": total_gst or "",
            "Cess": "",
            "Discount": "",
            "Freight/Delivery": other_adj or "",
            "Round Off": round_off or "",
            "Total": grand_total,
        })
        return rows

    for idx, product in enumerate(products):
        # Bill-level totals only on the first row so column sums make
        # sense — matches the sheet template the operations team uses.
        first = idx == 0
        rows.append({
            **common,
            "Item Name": getattr(product, "item_name", None) or getattr(product, "item_details", "") or "",
            "Item description": getattr(product, "item_details", "") or "",
            "Purchase Ledger": _ledger_name(getattr(product, "chart_of_accounts", None) or getattr(product, "taxes", None)),
            "Price": _num(getattr(product, "price", None)),
            "Qty": _num(getattr(product, "quantity", None)),
            "Amount": _num(getattr(product, "amount", 0)),
            "GST %": getattr(product, "product_gst", "") or "",
            "IGST": (igst_amt or "") if first else "",
            "IGST Ledger": igst_ledger if first else "",
            "CGST": (cgst_amt or "") if first else "",
            "CGST Ledger": cgst_ledger if first else "",
            "SGST": (sgst_amt or "") if first else "",
            "SGST Ledger": sgst_ledger if first else "",
            "Total GST": (total_gst or "") if first else "",
            "Cess": "",
            "Discount": "",
            "Freight/Delivery": (other_adj or "") if first else "",
            "Round Off": (round_off or "") if first else "",
            "Total": grand_total if first else "",
        })
    return rows


def rows_from_expense_analyzed(bill, analyzed_bill):
    """Return report rows for an Expense/Journal/Payment-style analyzed bill.

    Expense-side products don't carry price/qty/GST% — they're plain
    (ledger, amount, DR/CR) lines. Rendered same way: one row per line
    item, bill-level totals on the first row.
    """
    vendor = getattr(analyzed_bill, "vendor", None)
    vendor_name = _ledger_name(vendor) if vendor else ""
    gstin = ""
    for attr in ("gst_in", "gstin", "gst_number"):
        val = getattr(vendor, attr, None) if vendor else None
        if val:
            gstin = val
            break

    bill_id = (
        getattr(bill, "bill_munshi_name", None)
        or getattr(bill, "billmunshiName", None)
        or str(getattr(bill, "id", ""))
    )
    bill_no = getattr(analyzed_bill, "bill_no", "") or ""
    bill_date = _fmt_date(getattr(analyzed_bill, "bill_date", None))

    common = {
        "Bill Id": bill_id,
        "Vendor Name": vendor_name,
        "GSTIN": gstin,
        "Bill No": bill_no,
        "Bill Date": bill_date,
    }

    igst_amt = _num(getattr(analyzed_bill, "igst", 0))
    cgst_amt = _num(getattr(analyzed_bill, "cgst", 0))
    sgst_amt = _num(getattr(analyzed_bill, "sgst", 0))
    igst_ledger = _ledger_name(getattr(analyzed_bill, "igst_taxes", None))
    cgst_ledger = _ledger_name(getattr(analyzed_bill, "cgst_taxes", None))
    sgst_ledger = _ledger_name(getattr(analyzed_bill, "sgst_taxes", None))
    total_gst = sum(x for x in (igst_amt, cgst_amt, sgst_amt) if isinstance(x, (int, float)))
    round_off = _num(getattr(analyzed_bill, "round_off", 0))
    grand_total = _num(getattr(analyzed_bill, "total", 0))
    other_adj = _num(getattr(analyzed_bill, "other_adjustment", 0))

    try:
        products = list(analyzed_bill.products.all())
    except Exception:
        products = []

    rows = []
    if not products:
        rows.append({
            **common,
            "Item Name": "",
            "Item description": "",
            "Purchase Ledger": "",
            "Price": "",
            "Qty": "",
            "Amount": grand_total,
            "GST %": "",
            "IGST": igst_amt or "",
            "IGST Ledger": igst_ledger,
            "CGST": cgst_amt or "",
            "CGST Ledger": cgst_ledger,
            "SGST": sgst_amt or "",
            "SGST Ledger": sgst_ledger,
            "Total GST": total_gst or "",
            "Cess": "",
            "Discount": "",
            "Freight/Delivery": other_adj or "",
            "Round Off": round_off or "",
            "Total": grand_total,
        })
        return rows

    for idx, product in enumerate(products):
        first = idx == 0
        rows.append({
            **common,
            "Item Name": getattr(product, "item_details", "") or "",
            "Item description": getattr(product, "item_details", "") or "",
            "Purchase Ledger": _ledger_name(getattr(product, "chart_of_accounts", None)),
            "Price": "",
            "Qty": "",
            "Amount": _num(getattr(product, "amount", 0)),
            "GST %": "",
            "IGST": (igst_amt or "") if first else "",
            "IGST Ledger": igst_ledger if first else "",
            "CGST": (cgst_amt or "") if first else "",
            "CGST Ledger": cgst_ledger if first else "",
            "SGST": (sgst_amt or "") if first else "",
            "SGST Ledger": sgst_ledger if first else "",
            "Total GST": (total_gst or "") if first else "",
            "Cess": "",
            "Discount": "",
            "Freight/Delivery": (other_adj or "") if first else "",
            "Round Off": (round_off or "") if first else "",
            "Total": grand_total if first else "",
        })
    return rows

"""Arithmetic and identity checks on an extracted bill, whichever model read it.

The rules-based parser was dropped as a routing mechanism — on 96 real
client bills it served none of them. Its checks are the part worth
keeping, because they do not care who produced the numbers. A GPT-4o
reading can be internally inconsistent in exactly the ways a regex
reading can, and the same arithmetic catches both.

Run against the analysed-bill JSON the rest of the pipeline already uses
(``invoiceNumber``, ``total``, ``igst``, ``items`` …), not the parser's
own schema, so it applies to every bill the platform handles.

Nothing here blocks a bill. It produces findings for the operator to
look at, because the alternative — deciding on the user's behalf that a
bill is wrong — is how a correct bill gets stuck.
"""

import logging
import re

logger = logging.getLogger(__name__)

GSTIN_RE = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]\b")
GSTIN_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# Rupees. Below this a difference is rounding, not an error.
TOLERANCE = 1.0


def gstin_valid(value):
    """GSTN check digit: base-36, alternating weights 1 and 2."""
    g = (value or "").strip().upper()
    if not GSTIN_RE.fullmatch(g):
        return False
    total = 0
    for i, ch in enumerate(g[:14]):
        v = GSTIN_CHARS.index(ch) * (2 if i % 2 else 1)
        total += v // 36 + v % 36
    return GSTIN_CHARS[(36 - total % 36) % 36] == g[14]


def _num(value):
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").replace("₹", "").strip())
    except (TypeError, ValueError):
        return None


def _finding(code, message, **detail):
    return {"code": code, "message": message, **detail}


# "Continued to page 2" on a one-page upload. No extractor can find a
# total that is on a page nobody sent, so the fix is to ask for the rest
# of the document rather than to retry the reading.
CONTINUED_RE = re.compile(
    r"continued\s+(?:to|on)\s+page|cont(?:inued)?\.?\s*(?:to|on)?\s*next\s*page"
    r"|page\s+\d+\s+of\s+(\d+)", re.I)


def run_checks(data, document_text=None, page_count=None):
    """Findings for one analysed bill. Never raises; an empty list is a pass.

    Each finding carries a message written for the person reviewing the
    bill, not for a log. ``document_text`` is the OCR text when the bill
    came through that route; without it the text-based checks are skipped
    rather than guessed at.
    """
    try:
        return _run(data or {}, document_text, page_count)
    except Exception:
        logger.exception("Extraction checks failed; treating as no findings")
        return []


def _run(data, document_text=None, page_count=None):
    out = []

    if document_text:
        m = CONTINUED_RE.search(document_text)
        if m:
            stated = m.group(1)
            if not (stated and page_count and int(stated) <= page_count):
                out.append(_finding(
                    "continued_elsewhere",
                    "Continues on a page that was not uploaded.",
                    pages_uploaded=page_count,
                ))

    # --- identity -------------------------------------------------------
    seller_gstin = (data.get("from") or {}).get("gstin") or data.get("gstin")
    buyer_gstin = (data.get("to") or {}).get("gstin")
    for label, g in (("supplier", seller_gstin), ("customer", buyer_gstin)):
        if g and not gstin_valid(g):
            out.append(_finding(
                "gstin_checksum",
                f"{label.title()} GSTIN {g} fails its check digit.",
                value=g,
            ))
    if seller_gstin and buyer_gstin and seller_gstin.strip().upper() == buyer_gstin.strip().upper():
        out.append(_finding(
            "same_gstin",
            "Supplier and customer have the same GSTIN.",
        ))

    # --- line arithmetic ------------------------------------------------
    # Item vouchers (purchase, sales) carry their lines under "items";
    # ledger vouchers (expense, payment, receipt) carry theirs under
    # "expenses". Reading only "items" meant every ledger voucher was
    # told "No line items were found" however many lines it had — one
    # receipt in this corpus has eighteen — while the totals check,
    # which is gated on the same list, never ran on them at all. So the
    # advisory an operator saw on those vouchers was always wrong, and
    # the one worth seeing was never shown.
    #
    # An expense line is {description, category, amount}: no price or
    # quantity, so the rate x quantity test below skips it of its own
    # accord and only the sum is taken.
    items = data.get("items") or data.get("expenses") or []
    bad_lines = []
    line_sum = 0.0
    for n, item in enumerate(items, start=1):
        amount = _num(item.get("amount"))
        price, qty = _num(item.get("price")), _num(item.get("quantity"))
        if amount is not None:
            line_sum += amount
        if None in (amount, price, qty) or qty == 0:
            continue
        if abs(price * qty - amount) > TOLERANCE:
            bad_lines.append(n)
    if bad_lines:
        shown = ", ".join(str(n) for n in bad_lines[:5])
        more = f" and {len(bad_lines) - 5} more" if len(bad_lines) > 5 else ""
        out.append(_finding(
            "line_arithmetic",
            f"Rate x quantity does not match the amount on line {shown}{more}.",
            lines=bad_lines,
        ))

    # --- totals ---------------------------------------------------------
    total = _num(data.get("total"))
    igst, cgst, sgst = _num(data.get("igst")), _num(data.get("cgst")), _num(data.get("sgst"))
    tax = sum(v for v in (igst, cgst, sgst) if v)

    if total is not None and items:
        # The adjustments are part of the bill's own arithmetic, and
        # leaving them out made every discounted bill report a mismatch
        # of exactly its discount. Each is signed, so they simply add.
        adjustments = sum(
            v for v in (
                _num(data.get("cess")), _num(data.get("discount")),
                _num(data.get("freight")), _num(data.get("round_off")),
            ) if v
        )
        # MRP-priced lines already hold their GST; adding it again makes
        # the bill look wrong by exactly its own tax.
        expected = line_sum + adjustments
        if not data.get("lines_include_gst"):
            expected += tax
        if abs(expected - total) > TOLERANCE:
            out.append(_finding(
                "total_mismatch",
                f"Lines plus tax come to {expected:,.2f}; bill says {total:,.2f}.",
                computed=round(expected, 2), stated=round(total, 2),
            ))

    # The document names a currency other than the rupee. Said here
    # rather than acted on: setting the bill's currency without also
    # freezing a rate would leave every figure on it restated by
    # whatever the rate happens to be, so the operator is told and the
    # currency field on the voucher is where they answer.
    _ccy = str(data.get("currency") or "").strip().upper()
    if _ccy and _ccy != "INR":
        out.append(_finding(
            "currency_not_inr",
            f"Bill is in {_ccy}. Set the voucher currency.",
            currency=_ccy,
        ))

    if igst and (cgst or sgst):
        out.append(_finding(
            "igst_with_cgst_sgst",
            "IGST and CGST/SGST are both present.",
        ))
    if cgst and sgst and abs(cgst - sgst) > TOLERANCE:
        out.append(_finding(
            "cgst_sgst_unequal",
            f"CGST {cgst:,.2f} and SGST {sgst:,.2f} differ.",
        ))

    # --- completeness ---------------------------------------------------
    if not data.get("invoiceNumber"):
        out.append(_finding("no_invoice_number", "No invoice number was found."))
    if not data.get("dateIssued"):
        out.append(_finding("no_date", "No invoice date was found."))
    if not items:
        out.append(_finding("no_items", "No line items were found."))

    return out

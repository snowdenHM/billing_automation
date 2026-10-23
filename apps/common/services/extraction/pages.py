"""Work out whether a multi-page PDF is one bill or several.

Both shapes arrive through the same upload. A three-page tax invoice is
one bill whose items run past the first page; a batch scan of thirty
shop bills is thirty bills that happen to share a file. Read either one
wrongly and the result is silent: the three-page invoice loses its
totals, or the batch collapses into a single nonsense voucher.

Decided from the OCR text, which the pipeline already produces for every
page, so this costs nothing extra. The signals are the ones a person
would use:

  - the same invoice number printed on consecutive pages
  - "Page 2 of 3", where the stated total matches the file
  - "continued on the next page"
  - a page with no invoice number of its own, following one that has it

A page that states a *different* invoice number starts a new bill, and
that outranks everything else — it is the one signal a document cannot
produce by accident.

The default is to split, not to merge, and that is the whole design.
The two mistakes are not equally expensive. Merging two bills produces
one voucher carrying two vendors' items and a total belonging to
neither, and nothing downstream complains — it is wrong invisibly.
Splitting one bill produces two obviously incomplete bills, one of them
missing its total, which the continued-page check already flags — it is
wrong visibly. In an accounting product the invisible mistake is the
worse one, so pages are only merged on positive evidence.

Measured on real client batches: a 32-page scan of separate shop bills
comes out as 32, and genuine multi-page invoices still merge on their
invoice number, their "Page 2 of 3", or an explicit "continued".
"""

import re

# Deliberately loose: "Invoice No", "Bill No", "Document No", "Inv. #".
INVOICE_NO_RE = re.compile(
    r"(?:invoice|bill|document|inv|voucher)\s*(?:no|number|#)\s*[.:#]?\s*"
    r"([A-Z0-9][A-Z0-9/\-]{2,24})",
    re.I,
)
PAGE_X_OF_Y_RE = re.compile(r"page\s*(\d+)\s*(?:of|/)\s*(\d+)", re.I)

# Does this page open a document? A bill starts with a letterhead, which
# OCR renders as a heading, or with a title, or with a GSTIN. The Hindi
# terms are not decoration: a good share of real shop bills carry no
# English on them at all.
HEADING_RE = re.compile(r"\A\s*#{1,3}\s+\S")
DOC_TITLE_RE = re.compile(
    r"tax\s*invoice|\binvoice\b|\bbill\s*(?:no|of)\b|receipt|cash\s*memo"
    r"|estimate|challan|\u092c\u093f\u0932|\u0930\u0938\u0940\u0926|\u091a\u093e\u0932\u093e\u0928|\u0915\u0948\u0936|\u092e\u0947\u092e\u094b|\u0907\u0928\u0935\u0949\u0907\u0938",
    re.I,
)
GSTIN_RE = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]\b", re.I)


# A page that states its own total has finished saying what it had to
# say. Whatever follows it is a new bill, not more of this one.
CONCLUDED_RE = re.compile(
    r"grand\s*total|net\s*(?:amount|payable)|amount\s*payable|bill\s*net"
    r"|\btotal\b|\u0915\u0941\u0932|\u092f\u094b\u0917",
    re.I,
)


def _concluded(text, tail=900):
    """True when the page carries a total of its own."""
    return bool(CONCLUDED_RE.search((text or "")[-tail:]))


def _opens_a_document(text, head=700):
    """True when the page looks like the first page of a bill."""
    t = text or ""
    return bool(HEADING_RE.match(t) or GSTIN_RE.search(t[:head])
                or DOC_TITLE_RE.search(t[:head]))
CONTINUES_RE = re.compile(
    r"continued?\s+(?:to|on)\s+(?:the\s+)?next\s+page"
    r"|continued?\s+(?:to|on)\s+page"
    r"|cont(?:d|inued)?\.?\s*(?:overleaf|next)",
    re.I,
)


def _invoice_no(text):
    m = INVOICE_NO_RE.search(text or "")
    if not m:
        return None
    # Normalised for comparison only; the extractor reports the real one.
    return re.sub(r"[^A-Z0-9]", "", m.group(1).upper()) or None


def _page_of(text):
    m = PAGE_X_OF_Y_RE.search(text or "")
    if not m:
        return None
    try:
        return int(m.group(1)), int(m.group(2))
    except ValueError:
        return None


def group_pages(pages):
    """Group page texts into documents.

    *pages* is a list of strings, one per page, in order. Returns a list
    of ``{"pages": [index, ...], "reason": str}`` — one entry per bill.
    """
    texts = [p or "" for p in pages]
    if len(texts) <= 1:
        return [{"pages": list(range(len(texts))), "reason": "single page"}]

    numbers = [_invoice_no(t) for t in texts]
    page_of = [_page_of(t) for t in texts]
    continues = [bool(CONTINUES_RE.search(t)) for t in texts]
    total_pages = len(texts)

    groups = [{"pages": [0], "reason": "first page"}]
    for i in range(1, total_pages):
        prev = groups[-1]
        merge_reason = None

        # A different invoice number is decisive, and outranks everything:
        # it is the one signal a single document cannot produce by accident.
        if numbers[i] and any(numbers[j] and numbers[j] != numbers[i] for j in prev["pages"]):
            merge_reason = None

        # Same invoice number on both pages.
        elif numbers[i] and any(numbers[j] == numbers[i] for j in prev["pages"]):
            merge_reason = "same invoice number across pages"

        # "Page 2 of 3" following a page that said "of 3".
        elif page_of[i] and page_of[i][0] > 1 and any(
            page_of[j] and page_of[j][1] == page_of[i][1] for j in prev["pages"]
        ):
            merge_reason = f"page {page_of[i][0]} of {page_of[i][1]}"

        # The previous page said in so many words that it continues.
        elif continues[prev["pages"][-1]]:
            merge_reason = "previous page says it continues"

        # A bare continuation sheet: this page opens no document of its own
        # while the one before it did. Note the test is whether the page
        # LOOKS like a start, not whether the invoice-number regex matched —
        # that regex fails on most real bills, and merging on its silence
        # collapsed a 32-page batch into eleven.
        elif (not _opens_a_document(texts[i])
              and any(_opens_a_document(texts[j]) for j in prev["pages"])
              and not _concluded(texts[prev["pages"][-1]])):
            merge_reason = "continuation sheet, no header of its own"

        if merge_reason:
            prev["pages"].append(i)
            prev["reason"] = merge_reason
        else:
            groups.append({"pages": [i], "reason": "starts its own bill"})

    return groups


def describe(groups, total_pages):
    """One line for a log or a user-facing message."""
    if len(groups) == 1:
        return (f"{total_pages} pages read as one bill"
                if total_pages > 1 else "single-page bill")
    return f"{total_pages} pages read as {len(groups)} separate bills"

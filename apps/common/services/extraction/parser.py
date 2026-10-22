#!/usr/bin/env python3
"""Turn an indra-ocr scan of a GST tax invoice into a canonical JSON payload.

    python invoice_to_json.py paper.pdf              # JSON to stdout
    python invoice_to_json.py paper.pdf -o paper.json
    python invoice_to_json.py raw.jsonl              # reuse `indra-ocr --format json` output
    python invoice_to_json.py paper.pdf -o paper.json --annotate boxes/   # + page images with field boxes

From code (e.g. an API handler):

    from invoice_to_json import extract_invoice
    payload = extract_invoice("paper.pdf")

Payload: {schema_version, status, invoice, validation, meta}.  Every invoice
field is always present (null when the document does not carry it), dates are
ISO 8601, money is a number in the invoice currency.  Values the document does
not print but that follow from it (PAN from GSTIN, CGST/SGST split, tax
summary, ...) are listed in meta.derived_fields so callers can tell them apart.
"""

import argparse
import json
import os
import re
import statistics
import sys
import time
from datetime import datetime
from html.parser import HTMLParser

SCHEMA_VERSION = "1.0"

NUM = r"\d[\d,]*\.\d+"
UNITS = r"Kgs?|Gms?|Bags?|Pcs|Nos|Box|Ltrs?|Pkts?|Mtrs?|Dozen|Set|Qtl|Ton"
DATE = r"\d{1,2}[-/ ][A-Za-z]{3}[-/ ]\d{2,4}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}"
GSTIN = r"\b\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]\b"
GSTIN_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# unit as printed -> GST Unit Quantity Code
UQC = {
    "kg": "KGS", "kgs": "KGS", "gm": "GMS", "gms": "GMS", "bag": "BAG", "bags": "BAG",
    "pcs": "PCS", "nos": "NOS", "box": "BOX", "ltr": "LTR", "ltrs": "LTR",
    "pkt": "PAC", "pkts": "PAC", "mtr": "MTR", "mtrs": "MTR", "dozen": "DOZ",
    "set": "SET", "qtl": "QTL", "ton": "TON",
}

# boxed "label / value" fields; order matters (longer labels first)
LABELS = [
    ("irn", r"IRN"),
    ("ack_no", r"Ack\.?\s*No\.?"),
    ("ack_date", r"Ack\.?\s*Date"),
    ("invoice_no", r"Inv\w{2,4}\s*No\.?"),
    ("eway_bill_no", r"\S{0,2}-?\s*Way\s*B\w*\.?(\s*No\.?)?"),
    ("delivery_note_date", r"Delivery Note Date"),
    ("delivery_note", r"Delivery Note"),
    ("payment_terms", r"Mode/?\s*Terms of Paym\w*"),
    ("reference_no", r"Reference No\.?.*"),
    ("other_references", r"Other References?"),
    ("buyer_order_no", r"Buyer.s Order No\.?"),
    ("dispatch_doc_no", r"Dispatch Doc\w*\s*No\.?"),
    ("dispatched_through", r"Dispatched through"),
    ("destination", r"Destination"),
    ("lr_rr_no", r"Bill of Lading.*|LR-?RR No\.?"),
    ("vehicle_no", r"(Motor )?Vehicle No\.?"),
    ("terms_of_delivery", r"Terms of Delivery"),
    ("place_of_supply", r"Place of Supply"),
    ("reverse_charge", r"Reverse Charge"),
    ("dated", r"Dated|Date"),
]
DATED_OWNER = {"invoice_no": "invoice_date", "eway_bill_no": "invoice_date",
               "buyer_order_no": "buyer_order_date", "delivery_note": "delivery_note_date"}
PARTY_HEADERS = [
    ("consignee", r"Consignee|Ship(ped)?\s*to|Details of Consignee"),
    ("buyer", r"Buyer|Bill(ed)?\s*to|Invoice\s*to|Details of Receiver"
              r"|To(?=\s*[,:.])"),
]
# A party "name" that is really the box label above it -- the tell that the
# wrong block was read as a party.
LABELISH_NAME = re.compile(
    r"^(to|from|m/s\.?|buyer|seller|consignee|bill(ed)?\s*to|ship(ped)?\s*to"
    r"|invoice\s*to|party|name)\s*[,:.]?$", re.I)

TABLE_HEAD = (
    r"S[il1]\.?\s*No"            # Sl No / Si No / S1 No  (Tally)
    r"|S\.\s*No"                  # S.No
    r"|Sr\.?\s*No"                # Sr No / SR. NO
    r"|Description of Goods"      # Tally
    r"|Particul[ae]rs"            # PARTICULARS, and the common PARTICULERS typo
)


# --------------------------------------------------------------------- OCR input

def run_ocr(path):
    """[{index, lines: [{box, text, conf}], markdown, source, ms}] for a PDF/image."""
    import indra_ocr

    conv = indra_ocr.load(threads=indra_ocr.usable_cores())
    pages = []
    for i, page in conv.convert(path):
        pages.append({
            "index": i,
            "lines": [{"box": list(l.box), "text": l.text, "conf": l.conf} for l in page.lines],
            "markdown": page.markdown or "\n".join(l.text for l in page.lines),
            "source": page.source,
            "rotation": page.rotation,
            "ms": page.timings.get("total_ms"),
        })
    return pages, getattr(indra_ocr, "__version__", None)


def load_jsonl(path):
    pages = []
    with open(path, encoding="utf-8") as f:
        for row in filter(None, map(str.strip, f)):
            d = json.loads(row)
            pages.append({"index": d.get("page", len(pages)), "lines": d.get("lines", []),
                          "markdown": d.get("markdown") or "\n".join(l["text"] for l in d.get("lines", [])),
                          "source": d.get("source"), "rotation": d.get("rotation", 0),
                          "ms": (d.get("timings") or {}).get("total_ms")})
    return pages, None


def normalize(text):
    # OCR sometimes emits Cyrillic look-alikes (e.g. "Кg")
    return text.translate(str.maketrans("КкМмОоАаЕеСсРрТ’", "KkMmOoAaEeCcPpT'"))


# --------------------------------------------------------------------- helpers

def to_num(s):
    if s is None:
        return None
    s = re.sub(r"[^\d.\-]", "", str(s))
    try:
        return float(s)
    except ValueError:
        return None


def iso_date(s):
    if not s:
        return None
    s = re.sub(r"\s+", "-", s.strip())
    for fmt in ("%d-%b-%y", "%d-%b-%Y", "%d-%m-%Y", "%d-%m-%y", "%d/%m/%Y", "%d/%m/%y", "%d.%m.%Y"):
        try:
            return datetime.strptime(s.replace("/", "-") if "b" in fmt else s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def gstin_valid(g):
    """Check digit per the GSTN algorithm (base-36, alternating weights 1/2)."""
    if not g or not re.fullmatch(GSTIN, g):
        return False
    total = 0
    for i, ch in enumerate(g[:14]):
        v = GSTIN_CHARS.index(ch) * (2 if i % 2 else 1)
        total += v // 36 + v % 36
    return GSTIN_CHARS[(36 - total % 36) % 36] == g[14]


def find(pattern, text, group=1, flags=re.I | re.S):
    m = re.search(pattern, text, flags)
    return m.group(group).strip() if m else None


def r2(x):
    return None if x is None else round(x + 1e-9, 2)


class Line:
    __slots__ = ("x0", "y0", "x1", "y1", "text", "conf", "page")

    def __init__(self, d, page):
        self.x0, self.y0, self.x1, self.y1 = d["box"]
        self.text, self.conf, self.page = normalize(d["text"]).strip(), d["conf"], page

    @property
    def h(self):
        return self.y1 - self.y0


class TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables, self._row, self._cell = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.tables.append([])
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None and self.tables:
            self.tables[-1].append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


# --------------------------------------------------------------------- layout

class Layout:
    """OCR lines of one page with the geometry queries the extractors need."""

    def __init__(self, lines):
        self.lines = [l for l in lines if l.text]
        self.tags = {}  # id(line) -> what the line was used for (drives --annotate)
        self.lh = statistics.median([l.h for l in self.lines]) if self.lines else 20
        table = [l for l in self.lines if re.search(TABLE_HEAD, l.text, re.I)]
        self.table_top = min((l.y0 for l in table), default=float("inf"))

    def label_of(self, line):
        for key, pat in LABELS:
            m = re.match(rf"\s*({pat})\s*[:\-]?\s*(.*)$", line.text, re.I)
            if m:
                return key, m.groups()[-1].strip()
        return None, None

    def is_label(self, line):
        return self.label_of(line)[0] is not None or self.party_header(line) is not None

    def party_header(self, line):
        if self.label_of(line)[0]:  # "Buyer's Order No." is a field, not a section
            return None
        for key, pat in PARTY_HEADERS:
            if re.match(rf"\s*({pat})\b", line.text, re.I) and len(line.text) < 40:
                return key
        return None

    def right_of(self, l):
        cands = [o for o in self.lines if o is not l and abs((o.y0 + o.y1) / 2 - (l.y0 + l.y1) / 2) < l.h / 2
                 and 0 <= o.x0 - l.x1 < 6 * self.lh]
        return min(cands, key=lambda o: o.x0, default=None)

    def below(self, l, slack=2.0):
        cands = [o for o in self.lines if o is not l and 0 <= o.y0 - l.y1 < slack * self.lh
                 and min(o.x1, l.x1) - max(o.x0, l.x0) > 0.3 * min(o.x1 - o.x0, l.x1 - l.x0)]
        return min(cands, key=lambda o: o.y0, default=None)

    def tag(self, lines, what):
        for l in lines:
            self.tags.setdefault(id(l), what)

    def row_of(self, line):
        """Lines on the same table row as `line`."""
        yc = (line.y0 + line.y1) / 2
        return [o for o in self.lines if abs((o.y0 + o.y1) / 2 - yc) < 0.6 * self.lh and o.y0 >= self.table_top]

    def key_values(self):
        """{key: (value, [source lines])} for boxed label/value fields."""
        out = {}
        for l in self.lines:
            if l.y0 >= self.table_top:
                continue
            key, inline = self.label_of(l)
            if not key or key == "irn":
                continue
            if key == "dated":
                left = [o for o in self.lines if abs(o.y0 - l.y0) < l.h and o.x1 <= l.x0]
                owner = max(left, key=lambda o: o.x1, default=None)
                key = DATED_OWNER.get(self.label_of(owner)[0] if owner else None, "other_date")
            if inline:
                val = (inline, [l])
            else:
                nxt = next((o for o in (self.right_of(l), self.below(l)) if o and not self.is_label(o)), None)
                val = (nxt.text, [nxt]) if nxt else (None, [])
                self.tag([l], "label")
            if key not in out or out[key][0] is None:
                out[key] = val
                self.tag(val[1], "field")
        irn = next((l for l in self.lines if re.fullmatch(r"IRN\s*:?", l.text)), None)
        if irn:
            # 64 hex chars wrapped over lines, "-" at each break
            parts, cur, n = [], self.right_of(irn), 0
            while cur and n < 64 and re.fullmatch(r"[0-9a-f]+-?", cur.text):
                parts.append(cur)
                n += len(cur.text.rstrip("-"))
                cur = self.below(cur, slack=1.0)
            if parts:
                out["irn"] = (re.sub(r"-", "", "".join(p.text for p in parts)), parts)
                self.tag([irn], "label")
                self.tag(parts, "field")
        return out

    def block(self, start, down_only=False):
        """Lines in the same column as `start`, vertically contiguous."""
        col = [o for o in self.lines if abs(o.x0 - start.x0) < 2 * self.lh and o.y0 < self.table_top]
        col.sort(key=lambda o: o.y0)
        if start not in col:
            # `start` sits below the table (a GSTIN quoted in the footer
            # terms, say) so it is not in any column above it. One line is
            # a valid block; raising here would lose the whole document.
            return [start]
        i = col.index(start)
        lo = i
        while not down_only and lo > 0 and start.y0 - col[lo - 1].y1 < 2.5 * self.lh * (i - lo + 1) \
                and col[lo].y0 - col[lo - 1].y1 < 2.5 * self.lh and not self.is_label(col[lo - 1]):
            lo -= 1
        hi = i
        while hi + 1 < len(col) and col[hi + 1].y0 - col[hi].y1 < 2.5 * self.lh and not self.is_label(col[hi + 1]):
            hi += 1
        return col[lo:hi + 1]


# --------------------------------------------------------------------- extractors

def empty_party():
    return {"name": None, "gstin": None, "pan": None, "address": {
        "lines": [], "city": None, "state": None, "state_code": None, "pincode": None, "country": "IN"},
        "email": None, "phone": None}


def parse_party(lines, rec, prefix, derived):
    p = empty_party()
    if not lines:
        return p
    p["name"] = re.sub(r"\s*:\s*$", "", lines[0].text)
    rec(f"{prefix}.name", [lines[0]])
    addr = p["address"]
    addr_lines = []
    for l in lines[1:]:
        t = l.text
        if g := find(GSTIN, t, 0, 0):
            p["gstin"] = g
            rec(f"{prefix}.gstin", [l])
        elif m := re.search(r"State Name\s*:?\s*([^,]+)(?:,\s*Code\s*:?\s*(\d+))?", t, re.I):
            addr["state"], addr["state_code"] = m.group(1).strip(), m.group(2)
            rec(f"{prefix}.address.state", [l])
        elif e := find(r"E-?Mail\s*(?:ID)?\s*:?\s*(\S+@\S+)", t):
            p["email"] = e
            rec(f"{prefix}.email", [l])
        elif ph := find(r"(?:Ph(?:one)?|Mob(?:ile)?|Contact|Tel)\.?\s*(?:No\.?)?\s*:?\s*([+\d][\d\s\-]{7,})", t):
            p["phone"] = re.sub(r"\s", "", ph)
            rec(f"{prefix}.phone", [l])
        elif re.match(r"(PAN|GSTIN|State|Code)\b", t, re.I):
            continue
        elif p["gstin"] is None:
            addr["lines"].append(t)
            addr_lines.append(l)
    if addr_lines:
        rec(f"{prefix}.address.lines", addr_lines)
    joined = " ".join(addr["lines"])
    if pin := find(r"\b(\d{6})\b", joined):
        addr["pincode"] = pin
    if p["gstin"]:
        p["pan"] = p["gstin"][2:12]
        derived.append(f"{prefix}.pan")
        if not addr["state_code"]:
            addr["state_code"] = p["gstin"][:2]
            derived.append(f"{prefix}.address.state_code")
    # city: last word before the state name, or a lone one-word address line
    state = (addr["state"] or "").lower()
    lines_l = [x for x in addr["lines"] if not re.fullmatch(r"\d{6}", x)]
    for i, x in enumerate(lines_l):
        if state and x.lower().startswith(state) and i > 0:
            addr["city"] = lines_l[i - 1].split()[-1]
        elif state and state in x.lower() and x.lower().index(state) > 0:
            addr["city"] = x[: x.lower().index(state)].split()[-1]
    if not addr["city"] and len(lines_l) == 1 and len(lines_l[0].split()) <= 2:
        addr["city"] = lines_l[0]
    if addr["city"]:
        derived.append(f"{prefix}.address.city")
    return p


def parties(layout, rec, derived):
    out, used = {}, set()
    for l in layout.lines:
        key = layout.party_header(l)
        if key and key not in out:
            first = layout.below(l, slack=3.0)
            blk = layout.block(first, down_only=True) if first else []
            out[key] = parse_party(blk, rec, key, derived)
            used.update(map(id, blk))
            layout.tag([l], "label")
            layout.tag(blk, key)
    gst_lines = [l for l in layout.lines if re.search(GSTIN, l.text) and id(l) not in used]
    seller_blk = layout.block(gst_lines[0]) if gst_lines else []
    out["seller"] = parse_party(seller_blk, rec, "seller", derived)
    layout.tag(seller_blk, "seller")
    for k in ("seller", "buyer", "consignee"):
        out.setdefault(k, empty_party())
    return out


def parse_item(cells, line_no, warnings):
    desc = cells[1] if len(cells) > 1 else ""
    rest = " ".join(cells[2:])
    hsn = (find(r"\b(\d{4,8})\b(?!\.\d)", rest)
           or find(r"\(\s*(\d{4,8})\s*\)", desc)
           or find(r"\b(\d{4,8})\s*$", desc))
    if hsn:
        rest = rest.replace(hsn, " ", 1)
    tokens = re.findall(rf"({NUM})\s*({UNITS})?\b", rest, re.I)
    if len(tokens) == 2:
        # Only two money columns were found. NUM requires a decimal point, so
        # a quantity written as a bare integer -- often glued to a unit the
        # UNITS list does not know, like "9SQM" -- is invisible here and the
        # whole line used to be dropped. Recover it from the column text, and
        # fall back to amount/rate. Runs only where the line would be lost
        # anyway, so it cannot change a line that already parsed.
        rate_v, amount_v = to_num(tokens[0][0]), to_num(tokens[1][0])
        if rate_v:
            m = re.match(r"\s*(\d[\d,]*)\s*([A-Za-z]{1,6})?", rest)
            qty_txt = m.group(1) if m and m.group(1) else f"{amount_v / rate_v:g}"
            tokens = [(f"{to_num(qty_txt):g}.0", (m.group(2) if m else "") or "")] + tokens
    if len(tokens) < 3:
        warnings.append(f"line {line_no} ({desc!r}): could not split columns from {rest!r}")
        return None
    amount = to_num(tokens[-1][0])
    rate, per = to_num(tokens[-2][0]), tokens[-2][1]
    qty_tok = [t for t in tokens[:-2] if t[1]]
    qty, unit = (to_num(qty_tok[-1][0]), qty_tok[-1][1]) if qty_tok else (None, per)
    rate_incl = next((to_num(t[0]) for t in tokens[:-2] if not t[1]), None)
    disc = find(r"(\d+(?:\.\d+)?)\s*%", rest)
    desc_hsn = find(r"\(?\s*(\d{4,8})\s*\)?\s*$", desc)
    unit = unit or per
    item = {
        "line_no": line_no,
        "description": re.sub(r"\s*\(?\s*\d{4,8}\s*\)?\s*$", "", desc).strip(),
        "hsn_sac": hsn,
        "is_service": bool(hsn and hsn.startswith("99")),
        "quantity": qty,
        "unit": unit.capitalize() if unit else None,
        "uqc": UQC.get(unit.lower()) if unit else None,
        "alt_quantity": to_num(qty_tok[-2][0]) if len(qty_tok) > 1 else None,
        "alt_unit": qty_tok[-2][1].capitalize() if len(qty_tok) > 1 else None,
        "rate": rate,
        "rate_incl_tax": rate_incl,
        "per": (per or unit or "").capitalize() or None,
        "discount_pct": to_num(disc),
        "taxable_value": amount,
        "gst_rate": None,
        "cgst_amount": None,
        "sgst_amount": None,
        "igst_amount": None,
        "cess_amount": None,
        "total": None,
        "confidence": None,
    }
    if rate and rate_incl:
        item["gst_rate"] = r2((rate_incl / rate - 1) * 100)
    if qty and rate and amount is not None:
        gross = qty * rate * (1 - (item["discount_pct"] or 0) / 100)
        if abs(gross - amount) > 0.5:
            warnings.append(f"line {line_no} ({item['description']}): qty x rate = {r2(gross)} but amount = {amount}")
    if desc_hsn and hsn and not hsn.startswith(desc_hsn):
        warnings.append(f"line {line_no} ({item['description']}): HSN in description {desc_hsn} != HSN column {hsn}")
    return item


def parse_tables(pages, warnings):
    items, charges, totals_rows = [], [], {}
    for page in pages:
        tp = TableParser()
        tp.feed(normalize(page["markdown"]))
        for table in tp.tables:
            head = " ".join(" ".join(r) for r in table[:2])
            if re.search(r"Taxable\s*Value", head, re.I) and not re.search(r"Description", head, re.I):
                continue  # HSN-wise tax summary: recomputed from the items below
            for cells in table:
                joined = " ".join(cells)
                # An HSN-wise tax summary can start partway down a table,
                # after the Total and amount-in-words rows. Everything from
                # its header down restates the items above; read on and the
                # same money is counted twice as extra line items.
                if re.search(r"HSN\s*/?\s*SAC", joined, re.I) and re.search(r"Taxable", joined, re.I):
                    break
                if not re.search(NUM, joined) or re.search(r"Description|Kind|Pkgs", joined, re.I):
                    continue
                desc = (cells[1] if len(cells) > 1 else "").strip()
                last = to_num(find(rf"({NUM})\s*$", joined))
                # A two-column totals row is just ["TOTAL", "69030.00"], so
                # the label sits in the first cell. Wider tables put the
                # serial number there and the label in the second.
                first_cell = (cells[0] if cells else "").strip()
                if re.match(r"(Grand\s*)?Total\b", desc, re.I) \
                        or re.match(r"(Grand\s*)?Total\b", first_cell, re.I):
                    totals_rows["grand_total"] = last
                elif re.search(r"\b([CSI]GST|UTGST|CESS)\b|Round\w*\s*Off|Freight|Packing|Discount", desc, re.I):
                    charges.append({"name": desc, "amount": last, "page": page["index"]})
                elif not desc:
                    totals_rows.setdefault("subtotal", last)
                elif item := parse_item(cells, len(items) + 1, warnings):
                    item["_page"] = page["index"]
                    items.append(item)
    return items, charges, totals_rows


def footer_fields(text):
    t = text
    words = find(r"Amount Chargeable \(in words\)\s*:?\s*(.+?)(?:\n|E\.\s*&\s*O\.E|$)", t)
    return {
        "amount_in_words": words,
        "tax_amount_in_words": find(r"Tax Amount \(in words\)\s*:?\s*(.+?)(?:\n|$)", t),
        "bank": {
            "account_holder": find(r"A/c Holder'?s? Name\s*:?\s*([^\n]+)", t),
            "bank_name": find(r"Bank Name\s*:?\s*([^\n]+)", t),
            "account_no": find(r"A/c No\.?\s*:?\s*(\d[\d\s]{5,})", t),
            "branch": find(r"Branch\s*(?:&\s*IFS\w*\s*Code)?\s*:?\s*([^\n:]+?)(?:\s*&|\s+[A-Z]{4}0|\n|$)", t),
            "ifsc": find(r"\b([A-Z]{4}0[A-Z0-9]{6})\b", t, flags=0),
        },
        "company_pan": find(r"Company'?s PAN\s*:?\s*([A-Z]{5}\d{4}[A-Z])", t),
        "declaration": find(r"Declaration\s*:?\s*(.+?)(?:\n\n|for\s+[A-Z]|$)", t),
        "jurisdiction": find(r"SUBJECT TO (\w+) JURISDICTION", t),
        "computer_generated": bool(re.search(r"Computer Generated", t, re.I)),
        "continued": bool(re.search(r"continued to page", t, re.I)),
    }


# --------------------------------------------------------------------- assembly

def union_box(lines):
    return [r2(min(l.x0 for l in lines)), r2(min(l.y0 for l in lines)),
            r2(max(l.x1 for l in lines)), r2(max(l.y1 for l in lines))]


def build(pages, ocr_version=None, source_file=None, started=None, keep_layouts=None):
    warnings, derived, conf, boxes = [], [], {}, {}
    layouts = [Layout([Line(d, p["index"]) for d in p["lines"]]) for p in pages]
    if keep_layouts is not None:
        keep_layouts.extend(layouts)
    first = layouts[0] if layouts else Layout([])
    text = normalize("\n\n".join(p["markdown"] for p in pages))

    def rec(path, lines):
        if lines:
            conf[path] = min(l.conf for l in lines)
            boxes[path] = {"page": lines[0].page, "box": union_box(lines)}

    kv = {}
    for lay in layouts:
        for k, v in lay.key_values().items():
            if kv.get(k, (None,))[0] is None:
                kv[k] = v
    for k, (_, ls) in kv.items():
        rec(f"document.{k}", ls)

    def val(k):
        return kv.get(k, (None, None))[0]

    p = parties(first, rec, derived)
    items, charges, total_rows = parse_tables(pages, warnings)
    for c in charges:
        lay = layouts[c["page"]]
        hit = next((l for l in lay.lines if l.text.startswith(c["name"].split()[0])), None)
        if hit:
            row = lay.row_of(hit)
            lay.tag(row, "tax")
            rec(f"charges.{c['name'].split()[0].lower()}", row)
    foot = footer_fields(text)

    # e-way bill / invoice date often OCR-merged into one "Dated" box
    eway = find(r"(\d{12})", val("eway_bill_no") or "")
    inv_date_raw = find(rf"({DATE})", val("invoice_date") or "") or find(rf"Dated\s+(?:\d+\s+)?({DATE})", text)

    seller_sc = p["seller"]["address"]["state_code"]
    pos_raw = val("place_of_supply")
    pos_code = find(r"\b(\d{2})\b", pos_raw or "") or p["buyer"]["address"]["state_code"] \
        or p["consignee"]["address"]["state_code"]
    if not pos_raw and pos_code:
        derived.append("document.place_of_supply")
    def _charge_key(name):
        """CGST / SGST / IGST / CESS wherever it appears in the charge name.

        Truncating at the first non-word character only works for Tally,
        which labels the row exactly "CGST". "Output CGST @ 9%" keyed as
        OUTPUT, so the printed tax was invisible and the grand total never
        reconciled.
        """
        m = re.search(r"\b(CGST|SGST|UTGST|IGST|CESS)\b", name or "", re.I)
        return m.group(1).upper() if m else re.sub(r"\W.*", "", name or "").upper()

    explicit = {_charge_key(c["name"]): c["amount"] for c in charges}
    if "IGST" in explicit:
        supply = "inter_state"
    elif {"CGST", "SGST", "UTGST"} & explicit.keys():
        supply = "intra_state"
    else:
        supply = None if not (seller_sc and pos_code) else ("intra_state" if seller_sc == pos_code else "inter_state")
        if supply:
            derived.append("document.supply_type")

    # per-line tax and HSN-wise summary
    summary = {}
    for it in items:
        pg = it.pop("_page")
        it["bbox"] = None
        lay = layouts[pg] if pg < len(layouts) else None
        hit = next((l for l in lay.lines if it["description"] and l.text.startswith(it["description"])
                    and l.y0 >= lay.table_top), None) if lay else None
        if hit:
            row = lay.row_of(hit)
            lay.tag(row, "item")
            it["confidence"] = r2(min(l.conf for l in row))
            it["bbox"] = {"page": pg, "box": union_box(row)}
        if it["gst_rate"] is None or it["taxable_value"] is None:
            continue
        tax = it["taxable_value"] * it["gst_rate"] / 100
        if supply == "inter_state":
            it["igst_amount"] = r2(tax)
        else:
            it["cgst_amount"] = it["sgst_amount"] = r2(tax / 2)
        it["total"] = r2(it["taxable_value"] + tax)
        key = (it["hsn_sac"], it["gst_rate"])
        s = summary.setdefault(key, {"hsn_sac": key[0], "gst_rate": key[1], "taxable_value": 0.0,
                                     "cgst_amount": 0.0, "sgst_amount": 0.0, "igst_amount": 0.0})
        s["taxable_value"] += it["taxable_value"]
        for t in ("cgst_amount", "sgst_amount", "igst_amount"):
            s[t] += it[t] or 0
    if items:
        derived += ["line_items[].cgst_amount", "line_items[].sgst_amount", "line_items[].igst_amount",
                    "line_items[].total", "line_items[].gst_rate", "tax_summary"]
    tax_summary = [{k: r2(v) if isinstance(v, float) and k != "gst_rate" else v for k, v in s.items()}
                   for s in summary.values()]

    item_sum = r2(sum(i["taxable_value"] or 0 for i in items))
    calc = {t: r2(sum(i[t] or 0 for i in items)) for t in ("cgst_amount", "sgst_amount", "igst_amount")}
    taxes = {"cgst": explicit.get("CGST"), "sgst": explicit.get("SGST", explicit.get("UTGST")),
             "igst": explicit.get("IGST"), "cess": explicit.get("CESS")}
    for name, key in (("cgst", "cgst_amount"), ("sgst", "sgst_amount"), ("igst", "igst_amount")):
        if taxes[name] is None and calc[key] and not foot["continued"]:
            taxes[name] = calc[key]
            derived.append(f"totals.{name}")
    round_off = next((c["amount"] for c in charges if re.search(r"Round", c["name"], re.I)), None)
    other = r2(sum(c["amount"] or 0 for c in charges
                   if not re.search(r"\b([CSI]GST|UTGST|CESS)\b|Round", c["name"], re.I))) or None
    grand = total_rows.get("grand_total")
    # Printed tax wins where the document states it; per-line sums fill the
    # rest. Taking `taxes` alone would drop an axis the document left to the
    # lines, and `calc` alone drops one it stated only as a charge row.
    resolved_tax = {
        "cgst": taxes["cgst"] if taxes["cgst"] is not None else calc["cgst_amount"],
        "sgst": taxes["sgst"] if taxes["sgst"] is not None else calc["sgst_amount"],
        "igst": taxes["igst"] if taxes["igst"] is not None else calc["igst_amount"],
        "cess": taxes["cess"],
    }
    expected_grand = r2((total_rows.get("subtotal") or item_sum)
                        + sum(v or 0 for v in resolved_tax.values())
                        + (round_off or 0) + (other or 0))

    totals = {
        "line_count": len(items),
        "total_quantity": r2(sum(i["quantity"] or 0 for i in items)),
        "taxable_value": total_rows.get("subtotal") if total_rows.get("subtotal") is not None else item_sum,
        **taxes,
        "other_charges": other,
        "round_off": round_off,
        "grand_total": grand,
        "computed_grand_total": expected_grand,
        "amount_in_words": foot["amount_in_words"],
        "tax_amount_in_words": foot["tax_amount_in_words"],
    }
    derived.append("totals.computed_grand_total")

    invoice = {
        "document": {
            "type": "tax_invoice" if re.search(r"Tax Invoice", text, re.I) else "invoice",
            "is_e_invoice": bool(kv.get("irn") or re.search(r"e-Invoice", text, re.I)),
            "number": val("invoice_no"),
            "date": iso_date(inv_date_raw),
            "date_raw": inv_date_raw,
            "currency": "INR",
            "supply_type": supply,
            "place_of_supply": pos_raw or pos_code,
            "reverse_charge": (val("reverse_charge") or "").strip().lower() in ("yes", "y") if val("reverse_charge") else None,
            "irn": val("irn"),
            "ack_no": val("ack_no"),
            "ack_date": iso_date(find(rf"({DATE})", val("ack_date") or "")),
            "eway_bill": {"number": eway, "date": None},
        },
        "seller": p["seller"],
        "buyer": p["buyer"],
        "consignee": p["consignee"],
        "order_and_dispatch": {
            "buyer_order_no": val("buyer_order_no"),
            "buyer_order_date": iso_date(val("buyer_order_date")),
            "delivery_note": val("delivery_note"),
            "delivery_note_date": iso_date(val("delivery_note_date")),
            "reference_no": val("reference_no"),
            "other_references": val("other_references"),
            "payment_terms": val("payment_terms"),
            "dispatch_doc_no": val("dispatch_doc_no"),
            "dispatched_through": val("dispatched_through"),
            "destination": val("destination"),
            "lr_rr_no": val("lr_rr_no"),
            "vehicle_no": re.sub(r"\s", "", val("vehicle_no")) if val("vehicle_no") else None,
            "terms_of_delivery": val("terms_of_delivery"),
        },
        "line_items": items,
        "charges": [{k: v for k, v in c.items() if k != "page"} for c in charges],
        "tax_summary": tax_summary,
        "totals": totals,
        "bank": foot["bank"],
        "company_pan": foot["company_pan"],
        "declaration": foot["declaration"],
        "jurisdiction": foot["jurisdiction"],
        "computer_generated": foot["computer_generated"],
    }

    # ------------------------------------------------------------- validation
    checks = []

    def check(name, ok, **detail):
        checks.append({"name": name, "passed": bool(ok), **detail})

    for party in ("seller", "buyer", "consignee"):
        g = invoice[party]["gstin"]
        if g:
            check(f"{party}.gstin_checksum", gstin_valid(g), value=g)
            sc = invoice[party]["address"]["state_code"]
            if sc:
                check(f"{party}.gstin_matches_state_code", g[:2] == sc, gstin_state=g[:2], state_code=sc)
    if invoice["document"]["irn"]:
        check("document.irn_length_64", len(invoice["document"]["irn"]) == 64, length=len(invoice["document"]["irn"]))
    bad_lines = [i["line_no"] for i in items if i["quantity"] and i["rate"] and i["taxable_value"] is not None
                 and abs(i["quantity"] * i["rate"] * (1 - (i["discount_pct"] or 0) / 100) - i["taxable_value"]) > 0.5]
    check("line_items.qty_x_rate_equals_amount", not bad_lines, failed_lines=bad_lines)
    if total_rows.get("subtotal") is not None:
        check("totals.items_sum_equals_subtotal", abs(item_sum - total_rows["subtotal"]) <= 0.5,
              expected=total_rows["subtotal"], actual=item_sum)
    for name, key in (("cgst", "cgst_amount"), ("sgst", "sgst_amount"), ("igst", "igst_amount")):
        # Only reconcile when the document states the figure twice. Plenty
        # of invoices carry tax solely as a charge row and leave the lines
        # untaxed; comparing a printed 2669.49 against a per-line sum of
        # zero is not a disagreement, it is one statement and no other.
        if explicit.get(name.upper()) is not None and calc[key]:
            check(f"totals.{name}_matches_computed", abs(explicit[name.upper()] - calc[key]) <= 1,
                  printed=explicit[name.upper()], computed=calc[key])
    if grand is not None:
        check("totals.grand_total_matches_computed", abs(grand - expected_grand) <= 1, printed=grand, computed=expected_grand)

    seller_gstin = invoice["seller"]["gstin"]
    buyer_gstin = invoice["buyer"]["gstin"]
    if seller_gstin and buyer_gstin:
        check("parties.seller_differs_from_buyer", seller_gstin != buyer_gstin,
              seller=seller_gstin, buyer=buyer_gstin)
    seller_name = (invoice["seller"]["name"] or "").strip()
    if seller_name:
        # Two ways the wrong line gets read as a party name: the box label
        # above it ("To,"), or a sentence from the footer terms that merely
        # quotes a GSTIN. The second is the dangerous one -- the GSTIN is
        # right, so nothing else complains, but the vendor name is a
        # sentence.
        check("parties.seller_name_is_not_a_label",
              not LABELISH_NAME.match(seller_name), value=seller_name)
        check("parties.seller_name_looks_like_a_name",
              not re.search(r"\b(GST\w*|PAN|NUMBER|No\.)\b.*\b(is|:)\b|GSTIN|G\.S\.T", seller_name, re.I)
              and len(seller_name) <= 80,
              value=seller_name)

    missing = [f for f, v in (("document.number", invoice["document"]["number"]),
                              ("document.date", invoice["document"]["date"]),
                              ("seller.gstin", invoice["seller"]["gstin"]),
                              ("buyer.gstin", invoice["buyer"]["gstin"]),
                              ("totals.grand_total", grand)) if v is None]
    if not items:
        missing.append("line_items")
    if foot["continued"]:
        warnings.append("document says it continues on another page that was not provided; "
                        "taxes/totals there are missing")
    core_found = sum(1 for v in (invoice["document"]["number"], invoice["document"]["date"],
                                 seller_gstin, grand, items or None) if v)
    if not missing and all(c["passed"] for c in checks):
        status = "complete"
    elif core_found == 0:
        status = "failed"          # nothing usable came out; go straight to fallback
    elif missing:
        status = "partial"
    else:
        status = "needs_review"

    confs = [l.conf for lay in layouts for l in lay.lines]
    meta = {
        "core_fields_found": core_found,
        "source_file": source_file,
        "ocr_engine": "indra-ocr",
        "ocr_version": ocr_version,
        "pages": [{"index": p_["index"], "source": p_["source"], "ms": round(p_["ms"]) if p_["ms"] else None}
                  for p_ in pages],
        "page_count": len(pages),
        "processing_ms": round((time.time() - started) * 1000) if started else None,
        "mean_confidence": r2(statistics.mean(confs)) if confs else None,
        "field_confidence": {k: r2(v) for k, v in sorted(conf.items())},
        "field_boxes": dict(sorted(boxes.items())),  # [x0, y0, x1, y1] in page pixels at 200 dpi
        "image_dpi": 200,
        "derived_fields": sorted(set(derived)),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "invoice": invoice,
        "validation": {"checks": checks, "missing_fields": missing, "warnings": warnings},
        "meta": meta,
    }


COLORS = {  # tag -> RGB
    "seller": (37, 99, 235), "buyer": (22, 163, 74), "consignee": (13, 148, 136),
    "field": (217, 119, 6), "label": (148, 163, 184), "item": (147, 51, 234),
    "tax": (220, 38, 38), "other": (203, 213, 225),
}


def annotate(image_path, pages, layouts, payload, out_dir):
    """One PNG per page: every OCR line boxed, colored by what it was used for."""
    import numpy as np
    from PIL import Image, ImageDraw
    from indra_ocr import load_pages

    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(image_path))[0]
    written = []
    for (i, gray), page, lay in zip(load_pages(image_path, dpi=200), pages, layouts):
        if page.get("rotation"):
            gray = np.rot90(gray, k=page["rotation"] // 90)
        img = Image.fromarray(gray).convert("RGB")
        over = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(over)
        for l in lay.lines:
            c = COLORS[lay.tags.get(id(l), "other")]
            d.rectangle([l.x0, l.y0, l.x1, l.y1], fill=c + (40,), outline=c + (255,), width=2)
            if l.conf < 0.8:  # low-confidence reads get a dashed-looking red underline
                d.line([l.x0, l.y1 + 3, l.x1, l.y1 + 3], fill=(220, 38, 38, 255), width=3)
        for it in payload["invoice"]["line_items"]:
            if it["bbox"] and it["bbox"]["page"] == i:
                x0, y0, _, y1 = it["bbox"]["box"]
                d.text((x0 - 34, y0), f"#{it['line_no']}", fill=COLORS["item"] + (255,))
        x, y = 20, 20
        for name, c in COLORS.items():
            d.rectangle([x, y, x + 18, y + 18], fill=c + (255,))
            d.text((x + 24, y + 3), name, fill=(15, 23, 42, 255))
            x += 24 + 9 * len(name) + 20
        out = os.path.join(out_dir, f"{stem}_p{i + 1}.png")
        Image.alpha_composite(img.convert("RGBA"), over).convert("RGB").save(out)
        written.append(out)
    return written


def extract_invoice(path, annotate_dir=None):
    started = time.time()
    if path.endswith((".json", ".jsonl")):
        pages, version = load_jsonl(path)
    else:
        pages, version = run_ocr(path)
    layouts = []
    payload = build(pages, version, source_file=os.path.basename(path), started=started, keep_layouts=layouts)
    if annotate_dir:
        if path.endswith((".json", ".jsonl")):
            sys.exit("--annotate needs the original PDF/image as input")
        payload["meta"]["annotated_images"] = annotate(path, pages, layouts, payload, annotate_dir)
    return payload


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="invoice PDF/image, or JSON lines saved from `indra-ocr --format json`")
    ap.add_argument("-o", "--out", help="write JSON here instead of stdout")
    ap.add_argument("--annotate", metavar="DIR", help="also write page images with the field boxes drawn")
    args = ap.parse_args()

    out = json.dumps(extract_invoice(args.input, args.annotate), indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(out + "\n")
    else:
        print(out)


if __name__ == "__main__":
    main()

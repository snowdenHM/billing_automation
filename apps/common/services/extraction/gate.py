"""Decide whether a deterministic extraction is good enough to use.

The router asks one question of every parse: can this be trusted without
a human or a model looking at it? The answer has to be strict, because
the two failure modes are not equally expensive.

A parse that produced nothing is cheap — it is obviously empty, the bill
falls back, and a GPT-4o call costs a rupee or two. A parse that produced
a confident wrong answer is expensive: a buyer booked as the vendor
posts a wrong voucher to Tally, and nothing downstream complains. So the
gate fails closed. When in doubt, pay for the model.

Every reason is returned, not just the first, so tuning can see which
check actually costs the most throughput.
"""

from dataclasses import dataclass, field


# Checks the parser runs that the gate treats as disqualifying. Anything
# the parser reports as failed blocks the bill; these are named so the
# reason that comes back is readable rather than a raw check id.
CHECK_LABELS = {
    "seller.gstin_checksum": "seller GSTIN failed its checksum",
    "buyer.gstin_checksum": "buyer GSTIN failed its checksum",
    "consignee.gstin_checksum": "consignee GSTIN failed its checksum",
    "seller.gstin_matches_state_code": "seller GSTIN disagrees with the state code",
    "buyer.gstin_matches_state_code": "buyer GSTIN disagrees with the state code",
    "line_items.qty_x_rate_equals_amount": "a line's quantity x rate does not equal its amount",
    "totals.items_sum_equals_subtotal": "line items do not sum to the subtotal",
    "totals.grand_total_matches_computed": "the printed grand total disagrees with the computed one",
    "totals.cgst_matches_computed": "printed CGST disagrees with the computed figure",
    "totals.sgst_matches_computed": "printed SGST disagrees with the computed figure",
    "totals.igst_matches_computed": "printed IGST disagrees with the computed figure",
    "parties.seller_differs_from_buyer": "seller and buyer carry the same GSTIN",
    "parties.seller_name_is_not_a_label": "the seller name is a box label, not a party",
    "parties.seller_name_looks_like_a_name": "the seller name is a sentence, not a party",
    "document.irn_length_64": "the IRN is not 64 characters",
}

# Fields a voucher cannot be built without.
REQUIRED_FIELDS = ("document.number", "document.date", "totals.grand_total", "line_items")

# Mean OCR confidence below which the text itself is not trustworthy,
# whatever the arithmetic says. Starting point only — task 15 tunes this
# against the corpus, and it should move on measured precision, not taste.
MIN_MEAN_CONFIDENCE = 0.90

# Per-field floor for the fields a voucher is built from.
MIN_FIELD_CONFIDENCE = 0.80
GATED_FIELDS = ("document.invoice_no", "document.invoice_date", "seller.gstin", "seller.name")


@dataclass
class GateResult:
    passed: bool
    reasons: list = field(default_factory=list)
    # Everything the router wants to log, whichever way the decision went.
    metrics: dict = field(default_factory=dict)

    def __bool__(self):
        return self.passed


def evaluate(payload, *, min_mean_confidence=MIN_MEAN_CONFIDENCE,
             min_field_confidence=MIN_FIELD_CONFIDENCE):
    """Judge one ``invoice_to_json`` payload. Never raises.

    Returns a :class:`GateResult`. ``passed`` is True only when every
    condition holds; a malformed payload fails rather than erroring,
    because an exception here would take down the bill it was meant to
    protect.
    """
    reasons = []
    metrics = {}

    try:
        status = payload.get("status")
        invoice = payload.get("invoice") or {}
        validation = payload.get("validation") or {}
        meta = payload.get("meta") or {}
    except AttributeError:
        return GateResult(False, ["extraction payload was not a mapping"], {})

    metrics["status"] = status
    metrics["core_fields_found"] = meta.get("core_fields_found")

    # 1. The parser's own verdict.
    if status == "failed":
        reasons.append("extraction produced nothing usable")

    # 2. Required fields. The parser already lists what it could not find;
    # trust that rather than re-deriving it and risking disagreement.
    missing = [f for f in (validation.get("missing_fields") or []) if f in REQUIRED_FIELDS]
    metrics["missing_fields"] = missing
    for f in missing:
        reasons.append(f"{f} is missing")

    # 3. Arithmetic and identity. Any failed check disqualifies: these are
    # the only evidence that the numbers hang together.
    failed = [c for c in (validation.get("checks") or []) if not c.get("passed")]
    metrics["failed_checks"] = [c.get("name") for c in failed]
    for c in failed:
        name = c.get("name", "unknown check")
        reasons.append(CHECK_LABELS.get(name, f"check {name} failed"))

    # 4. At least one line item with a usable amount. A voucher with no
    # lines has nothing to post.
    items = invoice.get("line_items") or []
    metrics["line_items"] = len(items)
    if not items:
        reasons.append("no line items")
    elif not any(i.get("taxable_value") for i in items):
        reasons.append("no line item carries an amount")

    # 5. Confidence. Arithmetic can be self-consistent and still be built
    # on misread digits, so the OCR's own confidence is a separate gate.
    mean_conf = meta.get("mean_confidence")
    metrics["mean_confidence"] = mean_conf
    if mean_conf is None:
        reasons.append("no OCR confidence reported")
    elif mean_conf < min_mean_confidence:
        reasons.append(f"mean OCR confidence {mean_conf:.2f} is below {min_mean_confidence:.2f}")

    field_conf = meta.get("field_confidence") or {}
    low = {
        f: field_conf[f] for f in GATED_FIELDS
        if f in field_conf and field_conf[f] < min_field_confidence
    }
    metrics["low_confidence_fields"] = low
    for f, v in low.items():
        reasons.append(f"{f} confidence {v:.2f} is below {min_field_confidence:.2f}")

    return GateResult(not reasons, reasons, metrics)

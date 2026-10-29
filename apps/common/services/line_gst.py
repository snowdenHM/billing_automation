"""Give each line of a bill a GST rate, and tax that agrees with that rate.

The OCR reads a bill's *total* CGST/SGST/IGST reliably — it is printed in
the tax summary — but rarely reads a rate per line. Something still has
to decide what each line was taxed at, because Tally posts per line.

What this used to do was split the bill's tax across the lines in
proportion to their amounts, then read each line's rate back out of the
tax it had just been handed. On a single-rate bill that is harmless. On
a mixed-rate bill it is wrong twice over:

    Macroni  9,047.60 @ 5%  ->  452.38 tax
    POHA    14,400.00 @ 0%  ->      0

    proportional split of the 452.38:
    Macroni  452.38 x 9047.60/23447.60 = 174.56   (should be 452.38)
    POHA     452.38 x 14400.00/23447.60 = 277.82   (should be 0)

The exempt line is handed tax it must never carry, the taxable line is
short, and the rate derived from 277.82 on 14,400 is 1.93% — not a slab
at all, so it snaps to 0% and the row ends up claiming 0% while holding
277.82 of tax. That contradiction then flows into the GST rollup, the
voucher, and the return.

So this module works the other way round: decide the **rate** first,
then compute the tax from it. The invariant every caller can rely on is

    line.tax == line.amount * line.rate / 100

and it holds in every branch below, including the fallback.
"""

import logging
from decimal import ROUND_HALF_UP, Decimal

from apps.common.utils import STANDARD_GST_RATES

logger = logging.getLogger(__name__)

#: Line tax is matched against the bill's printed total to within this
#: much. Rounding on a per-line basis can legitimately drift a few paise
#: from the figure on the invoice; a rupee is comfortably past that and
#: still far below any real rate difference.
MATCH_TOLERANCE = Decimal("1.00")

#: Upper bound on the search below. Bills in this product run to a
#: handful of lines; the cap only stops a pathological 200-line import
#: from spending real time on a combinatorial search that will not help
#: it anyway.
MAX_SOLVE_LINES = 12
MAX_SOLVE_STATES = 20000

_PAISE = Decimal("0.01")


def _q(value):
    return Decimal(str(value)).quantize(_PAISE, rounding=ROUND_HALF_UP)


def _tax_for(amount, rate, inclusive=False):
    """Tax on *amount* at *rate* percent, to the paise.

    With *inclusive*, *amount* already contains the tax — an MRP-priced
    retail or pharmacy line — so the tax is the part of it that the
    rate accounts for, ``amount x r / (100 + r)``, not ``x r / 100``.
    """
    amount = Decimal(str(amount))
    rate = Decimal(str(rate))
    if inclusive:
        return _q(amount * rate / (Decimal("100") + rate))
    return _q(amount * rate / Decimal("100"))


def _split(total, inter_state):
    """One line's tax total, split into the three heads."""
    if inter_state:
        return {"igst": total, "cgst": _q(0), "sgst": _q(0)}
    half = _q(total / 2)
    # Put any odd paise on CGST so the two halves still add to the total
    # rather than quietly losing a paisa.
    return {"igst": _q(0), "cgst": half, "sgst": _q(total - half)}


def _solve_rates(amounts, target, candidates, inclusive=False):
    """Rates from *candidates* whose tax on *amounts* sums to *target*.

    Returns a list of rates, or None when nothing matches. Depth-first
    with pruning: a branch is abandoned as soon as its running tax is
    already past the target, which is what keeps a six-slab search over
    a realistic bill cheap.
    """
    n = len(amounts)
    if n == 0:
        return []
    if n > MAX_SOLVE_LINES:
        return None

    # Largest achievable tax from position i onwards, so a branch that
    # can no longer reach the target is dropped immediately.
    top = max(candidates)
    suffix_max = [Decimal("0")] * (n + 1)
    for i in range(n - 1, -1, -1):
        suffix_max[i] = suffix_max[i + 1] + _tax_for(amounts[i], top, inclusive)

    best = []
    states = [0]

    def walk(i, running, chosen):
        if states[0] > MAX_SOLVE_STATES:
            return False
        states[0] += 1
        if running - target > MATCH_TOLERANCE:
            return False
        if running + suffix_max[i] + MATCH_TOLERANCE < target:
            return False
        if i == n:
            if abs(running - target) <= MATCH_TOLERANCE:
                best.extend(chosen)
                return True
            return False
        for rate in candidates:
            if walk(i + 1, running + _tax_for(amounts[i], rate, inclusive), chosen + [rate]):
                return True
        return False

    return best if walk(0, Decimal("0"), []) else None


def assign_line_gst(lines, igst_total, cgst_total, sgst_total, taxable_base=None,
                    lines_include_gst=False):
    """Decide each line's GST rate, then its tax.

    *lines* is a list of ``{"amount": <number>, "rate": <number|None>}``.
    ``rate`` is whatever the OCR managed to read for that line (from an
    HSN column, a per-line rate column); ``None`` means unknown.

    *taxable_base* is the value GST was actually charged on, when that
    differs from the sum of the line amounts. Two things make it differ:

    * a **pre-tax (trade) discount**, which under CGST s.15(3)(a) is
      excluded from the taxable value, so the base is lower;
    * **taxable freight**, which under s.15(2)(c) is part of the taxable
      value, so the base is higher.

    When it is given, each line's taxable value is its share of that
    base, and its tax comes from that rather than from its gross
    amount. ``taxable_value`` is returned alongside the tax so a caller
    never has to re-derive it.

    Returns ``(results, strategy)`` where *results* mirrors *lines* as
    ``{"rate", "taxable_value", "igst", "cgst", "sgst"}`` and *strategy*
    names which branch decided it, for the log.

    *lines_include_gst* says the line amounts are tax-inclusive — the
    MRP case on a retail or pharmacy bill, where "397.05" already
    holds its 5%. The rate is then applied the other way round and
    ``taxable_value`` comes back lower than the amount. Without it such
    a bill has tax added on top of tax it already carried.
    """
    igst_total = _q(igst_total or 0)
    cgst_total = _q(cgst_total or 0)
    sgst_total = _q(sgst_total or 0)
    tax_total = _q(igst_total + cgst_total + sgst_total)

    # Which heads the bill actually used decides how every line splits.
    # Reading it off the bill beats re-deriving it from the two GSTINs:
    # the bill is the thing being recorded.
    inter_state = igst_total > 0 and (cgst_total + sgst_total) == 0

    amounts = [max(float(ln.get("amount") or 0), 0.0) for ln in lines]
    known = [ln.get("rate") for ln in lines]
    gross = sum(amounts)

    # Each line's share of the taxable base. With no adjustment this is
    # the line amount itself, so the common case is untouched.
    if taxable_base is None or gross <= 0:
        taxables = list(amounts)
    else:
        base = max(float(taxable_base), 0.0)
        taxables = [amount / gross * base for amount in amounts]

    def build(rates, strategy):
        out = []
        for taxable, rate in zip(taxables, rates):
            total = _tax_for(taxable, rate, lines_include_gst)
            # On an inclusive line the taxable value is what is left
            # once its own tax is taken out of the amount.
            value = _q(taxable) - total if lines_include_gst else _q(taxable)
            out.append({
                "rate": float(rate),
                "taxable_value": value,
                **_split(total, inter_state),
            })
        return out, strategy

    if not lines:
        return [], "empty"

    # 1. Every line's rate was read off the bill. Nothing to infer.
    if all(r is not None for r in known):
        return build([float(r) for r in known], "ocr-rates")

    # No tax at all on the bill — every line is zero-rated, whatever its
    # amount. Inferring a rate here would invent tax that is not there.
    if tax_total == 0:
        return build([0.0] * len(lines), "zero-rated")

    taxable_total = sum(taxables)
    if taxable_total <= 0:
        return build([0.0] * len(lines), "no-taxable-base")

    # 2. Single-rate bill: the blended rate is itself a slab. This is the
    #    common case and it is exact, so it is tried before any search.
    # Inclusive amounts hold the tax, so the rate they imply is
    # tax / (amount - tax), not tax / amount.
    if lines_include_gst:
        _net = taxable_total - float(tax_total)
        blended = float(tax_total) / _net * 100 if _net > 0 else 0.0
    else:
        blended = float(tax_total) / taxable_total * 100
    nearest = min(STANDARD_GST_RATES, key=lambda s: abs(s - blended))
    if abs(sum((_tax_for(t, nearest, lines_include_gst) for t in taxables), Decimal("0")) - tax_total) <= MATCH_TOLERANCE:
        return build([float(nearest)] * len(lines), "single-rate")

    # 3. Mixed rates. Hold the lines whose rate the OCR did read, and
    #    search slabs for the rest so the whole bill adds up.
    fixed_tax = Decimal("0")
    unknown_idx = []
    for i, rate in enumerate(known):
        if rate is None:
            unknown_idx.append(i)
        else:
            fixed_tax += _tax_for(taxables[i], float(rate), lines_include_gst)

    solved = _solve_rates(
        [taxables[i] for i in unknown_idx],
        tax_total - fixed_tax,
        [Decimal(str(s)) for s in STANDARD_GST_RATES],
        lines_include_gst,
    )
    if solved is not None:
        rates = [float(r) if r is not None else 0.0 for r in known]
        for pos, i in enumerate(unknown_idx):
            rates[i] = float(solved[pos])
        return build(rates, "solved-mixed")

    # 4. Nothing adds up — the bill's totals and its lines disagree, or
    #    there are too many lines to search. Fall back to the old
    #    proportional split, but give each line the rate its own share
    #    implies rather than snapping to a slab, so the row still says
    #    what it holds. The verify screen's "differs from bill" banner is
    #    what surfaces this to the operator.
    logger.warning(
        "Line GST did not resolve to slabs (tax %s over %s lines, base %s) — "
        "falling back to a proportional split.",
        tax_total, len(lines), taxable_total,
    )
    rates = []
    for taxable in taxables:
        if taxable <= 0:
            rates.append(0.0)
            continue
        share = Decimal(str(taxable)) / Decimal(str(taxable_total))
        line_tax = tax_total * share
        # ``build`` applies the rate the same way round as the bill, so
        # the rate handed back has to be derived that way too, or an
        # inclusive line would come out holding more tax than its share.
        divisor = (
            Decimal(str(taxable)) - line_tax if lines_include_gst
            else Decimal(str(taxable))
        )
        rates.append(float(line_tax / divisor * 100) if divisor > 0 else 0.0)
    return build(rates, "proportional-fallback")


def rate_from_hsn_summary(item, hsn_summary):
    """A line's GST rate, taken from the invoice's HSN summary table.

    The summary table is legally mandated on a GST tax invoice, states
    the rate per HSN explicitly, and is far more reliably read than a
    rate squeezed into a line row. Matching a line to it by HSN turns an
    inference into a reading.

    CGST and SGST are each half the rate, so they are added; IGST
    carries the full rate on its own. Returns None when the line has no
    HSN, the table has no matching row, or the row states no rate.
    """
    if not isinstance(item, dict) or not isinstance(hsn_summary, list):
        return None

    hsn = str(item.get("hsn") or "").strip()
    if not hsn:
        return None

    for row in hsn_summary:
        if not isinstance(row, dict):
            continue
        if str(row.get("hsn") or "").strip() != hsn:
            continue
        try:
            igst = float(row.get("igst_rate") or 0)
            cgst = float(row.get("cgst_rate") or 0)
            sgst = float(row.get("sgst_rate") or 0)
        except (TypeError, ValueError):
            return None
        rate = igst if igst > 0 else (cgst + sgst)
        # A genuine 0% row is a real answer, but only when the row
        # actually carried rate keys — an empty row means "unknown".
        if rate > 0:
            return rate
        if any(k in row for k in ("igst_rate", "cgst_rate", "sgst_rate")):
            return 0.0
    return None


def line_rate_from_payload(item, hsn_summary):
    """Best rate the extraction gave us for a line, or None.

    Order matters: the HSN summary beats a per-line ``gst_rate``,
    because the table is the authoritative statement of what was
    charged and the line row is a reading of a crowded column.
    """
    rate = rate_from_hsn_summary(item, hsn_summary)
    if rate is not None:
        return rate

    if isinstance(item, dict):
        raw = item.get("gst_rate")
        if raw not in (None, ""):
            try:
                value = float(raw)
            except (TypeError, ValueError):
                return None
            if value >= 0:
                return value
    return None

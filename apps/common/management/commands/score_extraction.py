"""Score an extractor against the corpus, field by field.

    python manage.py score_extraction --corpus ../corpus
    python manage.py score_extraction --corpus ../corpus --json report.json

Reports two numbers that answer different questions, and the difference
between them decides whether the router is worth building:

  coverage  — share of bills the gate lets through, i.e. how many would
              cost no model call.
  precision — of the bills the gate let through, the share whose fields
              actually match the human-verified labels.

Coverage is the saving. Precision is the risk. A high coverage with poor
precision is the worst outcome available: it posts wrong vendors to Tally
cheaply. Tune on precision; take whatever coverage falls out of it.
"""

import json
import os
import re
import time

from django.core.management.base import BaseCommand, CommandError

from apps.common.services.extraction import gate as gate_mod

# Fields scored, and how strictly. Money is compared numerically within a
# paisa; names are compared loosely because casing, punctuation and
# "PVT LTD" vs "Pvt. Ltd." are not extraction errors.
MONEY_FIELDS = {"total", "igst", "cgst", "sgst", "tds", "round_off"}
LOOSE_FIELDS = {"vendor"}
EXACT_FIELDS = {"bill_no", "bill_date", "due_date", "gst_type", "line_items"}


def _norm_loose(v):
    return re.sub(r"[^a-z0-9]", "", str(v or "").lower())


def _as_float(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def compare(field, expected, actual):
    """True / False, or None when the label does not state the field."""
    if expected in (None, "", "None"):
        return None
    if field in MONEY_FIELDS:
        e, a = _as_float(expected), _as_float(actual)
        return None if e is None else (a is not None and abs(e - a) <= 0.01)
    if field in LOOSE_FIELDS:
        e, a = _norm_loose(expected), _norm_loose(actual)
        return bool(e) and bool(a) and (e == a or e in a or a in e)
    return str(expected).strip() == str(actual or "").strip()


def payload_to_labels(payload):
    """The extractor payload in the same shape as ``expected.json``."""
    inv = payload.get("invoice") or {}
    doc, tot = inv.get("document") or {}, inv.get("totals") or {}
    return {
        "bill_no": doc.get("number"),
        "bill_date": doc.get("date"),
        "due_date": None,
        "gst_type": "IGST" if tot.get("igst") else ("CGST_SGST" if tot.get("cgst") else None),
        "total": tot.get("grand_total"),
        "igst": tot.get("igst"),
        "cgst": tot.get("cgst"),
        "sgst": tot.get("sgst"),
        "round_off": tot.get("round_off"),
        "vendor": (inv.get("seller") or {}).get("name"),
        "line_items": str(len(inv.get("line_items") or [])),
    }


class Command(BaseCommand):
    help = "Score the deterministic extractor against a labelled corpus."

    def add_arguments(self, parser):
        parser.add_argument("--corpus", required=True)
        parser.add_argument("--json", help="Write the full report here.")
        parser.add_argument("--limit", type=int)

    def handle(self, *args, **opts):
        root = os.path.abspath(opts["corpus"])
        folders = sorted(
            os.path.join(root, d) for d in os.listdir(root)
            if os.path.isdir(os.path.join(root, d))
        ) if os.path.isdir(root) else []
        if not folders:
            raise CommandError(f"No corpus entries under {root}. Run build_extraction_corpus first.")
        if opts["limit"]:
            folders = folders[: opts["limit"]]

        # Imported here, not at module load: it pulls in indra_ocr, which a
        # normal deployment does not install.
        from apps.common.services.extraction.parser import extract_invoice

        results, errors = [], 0
        started = time.time()
        for folder in folders:
            src = next((os.path.join(folder, f) for f in sorted(os.listdir(folder))
                        if f.startswith("bill.")), None)
            exp_path = os.path.join(folder, "expected.json")
            if not src or not os.path.exists(exp_path):
                continue
            expected = json.load(open(exp_path, encoding="utf-8"))

            try:
                payload = extract_invoice(src)
            except Exception as exc:
                errors += 1
                results.append({"id": os.path.basename(folder), "error": str(exc)[:200],
                                "gate_passed": False, "fields": {}})
                continue

            verdict = gate_mod.evaluate(payload)
            actual = payload_to_labels(payload)
            fields = {
                f: compare(f, expected.get(f), actual.get(f))
                for f in (MONEY_FIELDS | LOOSE_FIELDS | EXACT_FIELDS)
            }
            results.append({
                "id": os.path.basename(folder),
                "gate_passed": verdict.passed,
                "gate_reasons": verdict.reasons,
                "fields": {k: v for k, v in fields.items() if v is not None},
            })

        self._report(results, errors, time.time() - started)
        if opts["json"]:
            with open(opts["json"], "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2, default=str)
            self.stdout.write(f"\nFull report: {opts['json']}")

    def _report(self, results, errors, secs):
        n = len(results)
        passed = [r for r in results if r["gate_passed"]]
        w = self.stdout.write

        w(f"\n{n} bill(s) scored in {secs:.0f}s, {errors} raised\n")
        w(f"  coverage   {len(passed)}/{n} = {len(passed) / n:.1%}"
          f"   (served with no model call)")

        # Precision: among the bills the gate accepted, how many got every
        # stated field right. This is the number that must not be traded away.
        clean = [r for r in passed if r["fields"] and all(r["fields"].values())]
        if passed:
            w(f"  precision  {len(clean)}/{len(passed)} = {len(clean) / len(passed):.1%}"
              f"   (accepted AND every field correct)")
            wrong = [r for r in passed if r["fields"] and not all(r["fields"].values())]
            if wrong:
                w(self.style.ERROR(
                    f"\n  {len(wrong)} bill(s) PASSED THE GATE WITH A WRONG FIELD — "
                    f"these are the dangerous ones:"))
                for r in wrong[:10]:
                    bad = [f for f, ok in r["fields"].items() if ok is False]
                    w(f"    {r['id']}: {', '.join(bad)}")
        else:
            w("  precision  n/a — the gate accepted nothing")

        w("\n  per-field accuracy across all bills (not just accepted):")
        tally = {}
        for r in results:
            for f, ok in r["fields"].items():
                hit, tot = tally.get(f, (0, 0))
                tally[f] = (hit + bool(ok), tot + 1)
        for f in sorted(tally, key=lambda k: tally[k][0] / tally[k][1]):
            hit, tot = tally[f]
            w(f"    {f:12s} {hit:4d}/{tot:<4d} {hit / tot:6.1%}")

        reasons = {}
        for r in results:
            for why in r.get("gate_reasons", []):
                reasons[why] = reasons.get(why, 0) + 1
        if reasons:
            w("\n  why the gate refused (most common first):")
            for why, c in sorted(reasons.items(), key=lambda kv: -kv[1])[:12]:
                w(f"    {c:4d}  {why}")

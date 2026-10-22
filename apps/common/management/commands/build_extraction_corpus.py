"""Build a labelled extraction corpus out of bills people already verified.

The labels are free. Every Verified or Synced bill carries two things: what
the extractor produced, and what a human corrected it to. The correction is
the ground truth, and accountants produce it as a by-product of normal work.
Nothing has to be labelled by hand, and the corpus keeps growing on its own.

    python manage.py build_extraction_corpus --out ../corpus --limit 300

On de-identification, plainly: the labels are NOT scrubbed by default, and
that is deliberate. A scanned invoice cannot be redacted without destroying
the layout this corpus exists to test, so the source documents carry the real
values whatever we do. Scrubbing the labels beside them would buy no privacy
at all and would break the measurement — the extractor reads the real GSTIN
off the page, the label says a synthetic one, and a correct extraction scores
as a miss.

So the control is access and retention, not redaction. The command writes a
README saying so, and refuses to write inside a git work tree unless forced.
``--scrub-labels`` exists for the narrow case of sharing labels alone, and
makes exactly the fields it touches untestable.
"""

import hashlib
import json
import os
import random
import re
import shutil
from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError

from apps.common.services.activity import snapshot_bill_fields

# (label, model path, analysed-bill related name)
BILL_KINDS = [
    ("vendor", "apps.module.tally.models.TallyVendorBill", "tallyvendoranalyzedbill"),
    ("expense", "apps.module.tally.models.TallyExpenseBill", "tallyexpenseanalyzedbill"),
    ("payment", "apps.module.tally.models.TallyPaymentBill", "tallypaymentanalyzedbill"),
]

README = """\
Extraction corpus — handling rules
==================================

The source documents in here are real client bills, and they are NOT
redacted — redacting a scanned invoice destroys the layout this corpus exists
to test. The labels beside them are not scrubbed either by default, because
scrubbing a label while shipping the page it was read from buys no privacy
and makes that field impossible to score.

So the control is access and retention, not redaction.

  - Do not commit this directory. Do not copy it outside the machines agreed
    for the accuracy work.
  - Delete it when the measurement it was built for is finished.
  - Bills from healthcare providers can carry patient details that belong to
    the patient, not to the client whose consent covers this corpus. Treat
    anything under a provider name with that in mind.

Built by `manage.py build_extraction_corpus`.
"""


def _import(path):
    mod, _, name = path.rpartition(".")
    return getattr(__import__(mod, fromlist=[name]), name)


def _fake_gstin(seed):
    """A GSTIN-shaped value with a valid checksum, stable for one real value.

    Scrubbing to a constant would be simpler, but the corpus has to exercise
    the checksum validation, and the same vendor must keep the same fake
    GSTIN across bills or per-vendor grouping stops meaning anything.
    """
    rng = random.Random(seed)
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    state = f"{rng.randint(1, 37):02d}"
    pan = ("".join(rng.choice(chars[10:]) for _ in range(5))
           + f"{rng.randint(0, 9999):04d}"
           + rng.choice(chars[10:]))
    body = f"{state}{pan}{rng.choice('123456789')}Z"
    total = 0
    for i, ch in enumerate(body):
        v = chars.index(ch) * (2 if i % 2 else 1)
        total += v // 36 + v % 36
    return body + chars[(36 - total % 36) % 36]


def _scrub(value, salt):
    """Replace identifying text, keeping the shape the parser is tested on."""
    if not isinstance(value, str) or not value.strip():
        return value
    out = value
    out = re.sub(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]\b",
                 lambda m: _fake_gstin(salt + m.group(0)), out)
    out = re.sub(r"\b[A-Z]{5}\d{4}[A-Z]\b", "ABCDE1234F", out)
    out = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "party@example.com", out)
    out = re.sub(r"(?<!\d)(?:\+91[\s-]?)?[6-9]\d{9}(?!\d)", "9000000000", out)
    return out


class Command(BaseCommand):
    help = "Export a labelled extraction corpus from verified bills."

    def add_arguments(self, parser):
        parser.add_argument("--out", required=True, help="Directory to write into.")
        parser.add_argument("--limit", type=int, default=300)
        parser.add_argument(
            "--max-per-vendor", type=int, default=5,
            help="Stops one high-volume vendor from being most of the corpus.",
        )
        parser.add_argument("--organization", help="Restrict to one organization id.")
        parser.add_argument(
            "--scrub-labels", action="store_true",
            help=(
                "Replace GSTIN, PAN, email and phone in expected.json. Only for "
                "sharing labels apart from the documents: it makes those fields "
                "untestable, because the page still shows the real value."
            ),
        )
        parser.add_argument(
            "--force", action="store_true",
            help="Write even if the destination is inside a git work tree.",
        )

    def handle(self, *args, **opts):
        out = os.path.abspath(opts["out"])
        self._refuse_if_tracked(out, opts["force"])
        os.makedirs(out, exist_ok=True)

        rows = self._collect(opts)
        if not rows:
            raise CommandError("No Verified or Synced bills matched.")

        written = 0
        for n, (kind, bill, analysed) in enumerate(rows, start=1):
            folder = os.path.join(out, f"{n:04d}")
            os.makedirs(folder, exist_ok=True)
            if not self._copy_source(bill, folder):
                shutil.rmtree(folder, ignore_errors=True)
                continue

            expected = snapshot_bill_fields(analysed)
            if opts["scrub_labels"]:
                salt = str(bill.organization_id)
                expected = {k: _scrub(v, salt) for k, v in expected.items()}

            self._write(folder, "expected.json", expected)
            self._write(folder, "meta.json", {
                "bill_type": kind,
                # Hashed: grouping by organisation stays possible without
                # the corpus naming the client it came from.
                "organization": hashlib.sha256(
                    str(bill.organization_id).encode()).hexdigest()[:12],
                "status": bill.status,
                "verified_at": bill.updated_at.isoformat() if bill.updated_at else None,
                "labels_scrubbed": opts["scrub_labels"],
            })
            written += 1

        with open(os.path.join(out, "README.txt"), "w", encoding="utf-8") as f:
            f.write(README)

        self.stdout.write(self.style.SUCCESS(
            f"{written} bill(s) written to {out}. Labels come from the human "
            f"correction at verify. Neither the documents nor "
            f"{'the labels are' if not opts['scrub_labels'] else 'the documents are'} "
            f"redacted — see README.txt for how to handle them."
        ))

    # -- internals ---------------------------------------------------------

    def _refuse_if_tracked(self, out, force):
        probe = out
        while probe and probe != os.path.dirname(probe):
            if os.path.isdir(os.path.join(probe, ".git")):
                if force:
                    self.stdout.write(self.style.WARNING(
                        f"{out} is inside the git work tree at {probe} — writing anyway."))
                    return
                raise CommandError(
                    f"{out} is inside the git work tree at {probe}. Real bills must not "
                    f"land in a repository; choose a path outside it, or pass --force."
                )
            probe = os.path.dirname(probe)

    def _collect(self, opts):
        """Verified/Synced bills, spread across vendors, newest first."""
        picked, per_vendor = [], defaultdict(int)
        for kind, model_path, related in BILL_KINDS:
            model = _import(model_path)
            qs = model.objects.filter(
                status__in=["Verified", "Synced"], file__isnull=False,
            ).select_related("organization").order_by("-updated_at")
            if opts["organization"]:
                qs = qs.filter(organization_id=opts["organization"])

            for bill in qs.iterator():
                if len(picked) >= opts["limit"]:
                    return picked
                analysed = getattr(bill, related, None)
                if analysed is None:
                    continue
                vendor = getattr(getattr(analysed, "vendor", None), "name", "") or "?"
                key = (bill.organization_id, vendor)
                if per_vendor[key] >= opts["max_per_vendor"]:
                    continue
                per_vendor[key] += 1
                picked.append((kind, bill, analysed))
        return picked

    def _copy_source(self, bill, folder):
        try:
            bill.file.open("rb")
        except Exception as exc:
            self.stderr.write(f"  skip {bill.pk}: cannot read file ({exc})")
            return False
        try:
            ext = os.path.splitext(bill.file.name)[1].lower() or ".pdf"
            with open(os.path.join(folder, f"bill{ext}"), "wb") as dst:
                shutil.copyfileobj(bill.file, dst)
        finally:
            bill.file.close()
        return True

    @staticmethod
    def _write(folder, name, payload):
        with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False, default=str)

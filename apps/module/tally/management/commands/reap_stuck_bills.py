"""Clear bills left stuck in ``is_processing=True``.

A bill is flagged ``is_processing=True`` the moment its analysis job is
queued, and the job itself clears the flag whether it succeeds or fails.
That leaves one hole: if the RQ worker dies mid-job — OOM, deploy restart,
SIGKILL — nothing ever runs the cleanup. The row then sits in Draft,
marked busy, with no error and no job that will ever finish it.

On the list UI that bill is indistinguishable from one legitimately
waiting its turn, so it is never retried and never noticed.

This command finds those rows and marks them failed with a message the
operator can act on. It deliberately only touches bills whose
``updated_at`` is older than the cutoff, so a job that is genuinely still
running is left alone.

    # crontab -e  — every 15 minutes
    */15 * * * * cd /path/to/billing_automation && \
        /path/to/.venv/bin/python manage.py reap_stuck_bills >> /var/log/billmunshi/reaper.log 2>&1

Dry-run first:

    python manage.py reap_stuck_bills --dry-run
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.module.tally.models import (
    TallyExpenseBill,
    TallyPaymentBill,
    TallyVendorBill,
)

# Comfortably past the RQ job timeout (600s) so a slow-but-alive job is
# never mistaken for a dead one.
DEFAULT_STALE_MINUTES = 30

STUCK_MESSAGE = (
    "Analysis stopped unexpectedly — the background worker did not finish. "
    "Press Retry to run it again."
)

MODELS = (
    ("Vendor bill", TallyVendorBill),
    ("Expense bill", TallyExpenseBill),
    ("Payment voucher", TallyPaymentBill),
)


class Command(BaseCommand):
    help = "Mark bills stuck in processing as failed so they can be retried."

    def add_arguments(self, parser):
        parser.add_argument(
            "--minutes",
            type=int,
            default=DEFAULT_STALE_MINUTES,
            help=(
                "How long a bill may stay in processing before it counts as "
                f"stuck (default: {DEFAULT_STALE_MINUTES})."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be reaped without changing anything.",
        )

    def handle(self, *args, **options):
        minutes = options["minutes"]
        dry_run = options["dry_run"]
        cutoff = timezone.now() - timedelta(minutes=minutes)

        self.stdout.write(
            f"Looking for bills processing since before "
            f"{cutoff:%Y-%m-%d %H:%M:%S} (>{minutes} min)."
        )

        total = 0
        for label, model in MODELS:
            stuck = model.objects.filter(
                is_processing=True, updated_at__lt=cutoff,
            )
            count = stuck.count()
            if not count:
                continue

            total += count
            self.stdout.write(f"  {label}: {count} stuck")
            for bill in stuck[:10]:
                name = getattr(bill, "bill_munshi_name", None) or bill.id
                self.stdout.write(f"    - {name} (updated {bill.updated_at:%H:%M:%S})")
            if count > 10:
                self.stdout.write(f"    … and {count - 10} more")

            if not dry_run:
                stuck.update(
                    is_processing=False,
                    processing_error=STUCK_MESSAGE,
                    processing_stage="failed",
                )

        if not total:
            self.stdout.write(self.style.SUCCESS("Nothing stuck."))
        elif dry_run:
            self.stdout.write(
                self.style.WARNING(f"Dry run — {total} bill(s) would be reaped.")
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(f"Reaped {total} stuck bill(s).")
            )

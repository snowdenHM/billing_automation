"""Destroy trashed bills whose 30-day retention window has elapsed.

Schedule this daily. Without it, trashed bills stay on disk forever — the
Trash UI stops *offering* an expired bill for restore (it filters on the
cutoff itself), but only this command reclaims the storage.

    # crontab -e  — 2:30am daily
    30 2 * * * cd /path/to/billing_automation && \
        /path/to/.venv/bin/python manage.py purge_trashed_bills >> /var/log/billmunshi/trash-purge.log 2>&1

Always dry-run first on a new deployment:

    python manage.py purge_trashed_bills --dry-run
"""

from django.core.management.base import BaseCommand, CommandError

from apps.common.models import get_trash_retention_days, trash_cutoff
from apps.module.tally.trash import TRASH_KINDS, get_kind, purge_expired


class Command(BaseCommand):
    help = "Permanently delete trashed Tally bills past the retention window."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be destroyed without destroying anything.",
        )
        parser.add_argument(
            "--type",
            dest="kind_slug",
            default=None,
            help=(
                "Restrict to one document type "
                f"({' | '.join(sorted(TRASH_KINDS))}). Defaults to all."
            ),
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        kind_slug = options["kind_slug"]

        if kind_slug:
            kind = get_kind(kind_slug)
            if not kind:
                raise CommandError(
                    f"Unknown type '{kind_slug}'. Valid types: {', '.join(sorted(TRASH_KINDS))}"
                )
            kinds = [kind]
        else:
            kinds = list(TRASH_KINDS.values())

        retention = get_trash_retention_days()
        cutoff = trash_cutoff()

        self.stdout.write(
            f"Retention: {retention} days — purging bills trashed on or before "
            f"{cutoff:%Y-%m-%d %H:%M:%S %Z}"
        )
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — nothing will be deleted"))

        results = purge_expired(kinds=kinds, dry_run=dry_run)

        total = 0
        for kind in kinds:
            count = results.get(kind.slug, 0)
            total += count
            self.stdout.write(f"  {kind.label:<20} {count}")

        verb = "would be purged" if dry_run else "purged"
        style = self.style.WARNING if dry_run else self.style.SUCCESS
        self.stdout.write(style(f"{total} bill(s) {verb}."))

"""Create the missing workspace for users who signed up before provisioning existed.

Registration used to store the signup form's organisation details as plain
text on the User (``signup_organization_name`` / ``signup_organization_gst``
/ ``signup_accounting_software``) without ever creating an Organization or
granting a membership. Those accounts sign in to an empty Clients list —
that page lists organisations the user has an active membership in — and
have nowhere to upload a bill.

New signups are handled at registration. This backfills the ones already
in the database.

Only users who have NO active membership at all are touched, so anyone who
was later added to a workspace by hand is left alone.

    python manage.py backfill_signup_organizations --dry-run
    python manage.py backfill_signup_organizations
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from apps.organizations.services import provision_organization

User = get_user_model()


class Command(BaseCommand):
    help = "Create Organization + admin membership for users who signed up without one."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be created without writing anything.",
        )
        parser.add_argument(
            "--include-nameless",
            action="store_true",
            help=(
                "Also provision users who left the organisation name blank. "
                "They get a workspace named after their email local part."
            ),
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        include_nameless = options["include_nameless"]

        candidates = (
            User.objects.filter(is_active=True)
            .exclude(memberships__is_active=True)
            .order_by("date_joined")
        )

        total = created = skipped = failed = 0

        for user in candidates:
            total += 1
            name = (getattr(user, "signup_organization_name", "") or "").strip()

            if not name:
                if not include_nameless:
                    skipped += 1
                    self.stdout.write(
                        f"  skip  {user.email} — no organisation name on signup"
                    )
                    continue
                # Something recognisable beats "My Organization" for every
                # one of them.
                name = user.email.split("@")[0]

            if dry_run:
                created += 1
                self.stdout.write(f"  would create  {name!r}  for {user.email}")
                continue

            try:
                org = provision_organization(
                    user,
                    name=name,
                    gst_number=getattr(user, "signup_organization_gst", "") or "",
                    accounting_software=getattr(
                        user, "signup_accounting_software", "",
                    ) or "",
                )
            except Exception as exc:
                failed += 1
                self.stderr.write(
                    self.style.ERROR(f"  FAILED  {user.email}: {exc}")
                )
                continue

            if org is None:
                skipped += 1
                continue

            created += 1
            self.stdout.write(
                self.style.SUCCESS(f"  created  {org.name!r}  for {user.email}")
            )

        self.stdout.write("")
        self.stdout.write(
            f"  {total} user(s) without a workspace · "
            f"{created} {'would be ' if dry_run else ''}provisioned · "
            f"{skipped} skipped · {failed} failed"
        )
        if dry_run:
            self.stdout.write(self.style.WARNING("  Dry run — nothing written."))

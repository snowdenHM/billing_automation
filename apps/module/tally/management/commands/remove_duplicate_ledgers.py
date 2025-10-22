# apps/module/tally/management/commands/remove_duplicate_ledgers.py

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count
from apps.organizations.models import Organization
from apps.module.tally.models import Ledger
import uuid


class Command(BaseCommand):
    help = 'Remove duplicate ledgers for a specific organization based on master_id'

    def add_arguments(self, parser):
        parser.add_argument(
            'organization_id',
            type=str,
            help='Organization UUID to remove duplicate ledgers from'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be deleted without actually deleting',
        )
        parser.add_argument(
            '--verbose',
            action='store_true',
            help='Show detailed information about duplicates found',
        )

    def handle(self, *args, **options):
        organization_id = options['organization_id']
        dry_run = options['dry_run']
        verbose = options['verbose']

        # Validate organization ID format
        try:
            uuid.UUID(organization_id)
        except ValueError:
            raise CommandError(f'Invalid UUID format for organization_id: {organization_id}')

        # Get organization
        try:
            organization = Organization.objects.get(id=organization_id)
        except Organization.DoesNotExist:
            raise CommandError(f'Organization with ID {organization_id} does not exist')

        self.stdout.write(
            self.style.SUCCESS(f'Processing organization: {organization.name} (ID: {organization.id})')
        )

        # Find duplicates based on master_id
        duplicates = (
            Ledger.objects
            .filter(organization=organization)
            .exclude(master_id__isnull=True)
            .exclude(master_id='')
            .values('master_id')
            .annotate(count=Count('id'))
            .filter(count__gt=1)
        )

        if not duplicates.exists():
            self.stdout.write(
                self.style.SUCCESS('No duplicate ledgers found for this organization.')
            )
            return

        total_duplicates = duplicates.count()
        total_to_delete = 0

        self.stdout.write(
            self.style.WARNING(f'Found {total_duplicates} master_ids with duplicate ledgers:')
        )

        duplicates_info = []

        # Process each duplicate master_id
        for duplicate in duplicates:
            master_id = duplicate['master_id']
            count = duplicate['count']

            # Get all ledgers with this master_id
            duplicate_ledgers = (
                Ledger.objects
                .filter(organization=organization, master_id=master_id)
                .order_by('created_at')  # Keep the oldest one
            )

            # Keep the first (oldest) and mark others for deletion
            ledgers_to_keep = duplicate_ledgers.first()
            ledgers_to_delete = duplicate_ledgers[1:]  # All except the first

            duplicates_info.append({
                'master_id': master_id,
                'count': count,
                'keep': ledgers_to_keep,
                'delete': list(ledgers_to_delete)
            })

            total_to_delete += len(ledgers_to_delete)

            if verbose:
                self.stdout.write(f'\n  Master ID: {master_id} ({count} duplicates)')
                self.stdout.write(f'    KEEP: {ledgers_to_keep.name} (ID: {ledgers_to_keep.id}, Created: {ledgers_to_keep.created_at})')

                for ledger in ledgers_to_delete:
                    self.stdout.write(
                        self.style.ERROR(f'    DELETE: {ledger.name} (ID: {ledger.id}, Created: {ledger.created_at})')
                    )

        self.stdout.write(f'\nSummary:')
        self.stdout.write(f'  - Total duplicate master_ids: {total_duplicates}')
        self.stdout.write(f'  - Total ledgers to delete: {total_to_delete}')
        self.stdout.write(f'  - Total ledgers to keep: {total_duplicates}')

        if dry_run:
            self.stdout.write(
                self.style.SUCCESS('\n[DRY RUN] No changes made. Use without --dry-run to actually delete duplicates.')
            )
            return

        # Confirm deletion
        if not options.get('verbosity', 1) == 0:  # Don't ask for confirmation in quiet mode
            confirm = input(f'\nAre you sure you want to delete {total_to_delete} duplicate ledgers? (yes/no): ')
            if confirm.lower() != 'yes':
                self.stdout.write(self.style.ERROR('Operation cancelled.'))
                return

        # Perform deletion
        deleted_count = 0
        with transaction.atomic():
            for duplicate_info in duplicates_info:
                for ledger in duplicate_info['delete']:
                    if verbose:
                        self.stdout.write(f'Deleting: {ledger.name} (ID: {ledger.id})')

                    ledger.delete()
                    deleted_count += 1

        self.stdout.write(
            self.style.SUCCESS(f'\nSuccessfully deleted {deleted_count} duplicate ledgers.')
        )

        # Verify cleanup
        remaining_duplicates = (
            Ledger.objects
            .filter(organization=organization)
            .exclude(master_id__isnull=True)
            .exclude(master_id='')
            .values('master_id')
            .annotate(count=Count('id'))
            .filter(count__gt=1)
        )

        if remaining_duplicates.exists():
            self.stdout.write(
                self.style.ERROR(f'Warning: {remaining_duplicates.count()} master_ids still have duplicates.')
            )
        else:
            self.stdout.write(
                self.style.SUCCESS('Verification: No duplicate ledgers remaining for this organization.')
            )

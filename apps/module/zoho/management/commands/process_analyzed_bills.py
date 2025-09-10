# apps/module/zoho/management/commands/process_analyzed_bills.py

import logging
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from datetime import datetime, timedelta

from apps.organizations.models import Organization
from apps.module.zoho.models import (
    VendorBill,
    VendorZohoBill,
    VendorZohoProduct,
    ExpenseBill,
    ExpenseZohoBill,
    ExpenseZohoProduct,
    ZohoCredentials,
    ZohoVendor,
    ZohoChartOfAccount,
    ZohoTaxes,
)

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Process analyzed bills (vendor and expense) and prepare them for verification'

    def add_arguments(self, parser):
        parser.add_argument(
            '--organization',
            type=str,
            help='Organization ID (UUID) to process bills for. If not provided, processes all organizations.'
        )
        parser.add_argument(
            '--bill-type',
            choices=['vendor', 'expense', 'all'],
            default='all',
            help='Type of bills to process: vendor, expense, or all (default: all)'
        )
        parser.add_argument(
            '--days-old',
            type=int,
            default=7,
            help='Only process bills analyzed in the last N days (default: 7)'
        )
        parser.add_argument(
            '--limit',
            type=int,
            help='Maximum number of bills to process per type'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be processed without making changes'
        )

    def handle(self, *args, **options):
        self.stdout.write(
            self.style.SUCCESS('Starting processed analyzed bills command...')
        )

        # Get options
        organization_id = options.get('organization')
        bill_type = options.get('bill_type')
        days_old = options.get('days_old')
        limit = options.get('limit')
        dry_run = options.get('dry_run')

        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN MODE - No changes will be made'))

        # Filter by date
        cutoff_date = timezone.now() - timedelta(days=days_old)

        # Get organizations to process
        if organization_id:
            try:
                organizations = [Organization.objects.get(id=organization_id)]
                self.stdout.write(f'Processing organization: {organizations[0].name}')
            except Organization.DoesNotExist:
                raise CommandError(f'Organization with ID {organization_id} does not exist')
        else:
            organizations = Organization.objects.all()
            self.stdout.write(f'Processing all {organizations.count()} organizations')

        total_processed = {'vendor': 0, 'expense': 0}
        total_errors = {'vendor': 0, 'expense': 0}

        for organization in organizations:
            self.stdout.write(f'\n--- Processing Organization: {organization.name} ({organization.id}) ---')

            # Check if organization has Zoho credentials
            try:
                credentials = ZohoCredentials.objects.get(organization=organization)
            except ZohoCredentials.DoesNotExist:
                self.stdout.write(
                    self.style.WARNING(f'Skipping organization {organization.name} - No Zoho credentials found')
                )
                continue

            # Process vendor bills
            if bill_type in ['vendor', 'all']:
                processed, errors = self.process_vendor_bills(
                    organization, cutoff_date, limit, dry_run
                )
                total_processed['vendor'] += processed
                total_errors['vendor'] += errors

            # Process expense bills
            if bill_type in ['expense', 'all']:
                processed, errors = self.process_expense_bills(
                    organization, cutoff_date, limit, dry_run
                )
                total_processed['expense'] += processed
                total_errors['expense'] += errors

        # Summary
        self.stdout.write(f'\n{self.style.SUCCESS("=== PROCESSING COMPLETE ===")}\n')
        self.stdout.write(f'Vendor Bills - Processed: {total_processed["vendor"]}, Errors: {total_errors["vendor"]}')
        self.stdout.write(f'Expense Bills - Processed: {total_processed["expense"]}, Errors: {total_errors["expense"]}')
        self.stdout.write(f'Total Processed: {sum(total_processed.values())}')
        self.stdout.write(f'Total Errors: {sum(total_errors.values())}')

    def process_vendor_bills(self, organization, cutoff_date, limit, dry_run):
        """Process analyzed vendor bills for an organization."""
        self.stdout.write(f'\n  🏢 Processing Vendor Bills...')

        # Get analyzed vendor bills
        queryset = VendorBill.objects.filter(
            organization=organization,
            status='Analysed',
            updated_at__gte=cutoff_date
        ).order_by('-updated_at')

        if limit:
            queryset = queryset[:limit]

        bills = list(queryset)
        self.stdout.write(f'    Found {len(bills)} analyzed vendor bills')

        processed = 0
        errors = 0

        for bill in bills:
            try:
                if dry_run:
                    self.stdout.write(f'    [DRY RUN] Would process vendor bill ID: {bill.id}')
                    processed += 1
                    continue

                with transaction.atomic():
                    # Check if VendorZohoBill already exists
                    try:
                        zoho_bill = VendorZohoBill.objects.get(
                            selectBill=bill,
                            organization=organization
                        )
                        self.stdout.write(f'    ✓ Vendor bill {bill.id} already has Zoho data')
                        processed += 1
                        continue
                    except VendorZohoBill.DoesNotExist:
                        pass

                    # Process analyzed data if available
                    if not bill.analysed_data:
                        self.stdout.write(f'    ⚠️  Vendor bill {bill.id} has no analyzed data')
                        continue

                    # Create Zoho objects from analyzed data
                    self.create_vendor_zoho_objects(bill, bill.analysed_data, organization)

                    self.stdout.write(f'    ✓ Processed vendor bill ID: {bill.id}')
                    processed += 1

            except Exception as e:
                errors += 1
                self.stdout.write(
                    self.style.ERROR(f'    ✗ Error processing vendor bill {bill.id}: {str(e)}')
                )
                logger.error(f'Error processing vendor bill {bill.id}: {str(e)}', exc_info=True)

        return processed, errors

    def process_expense_bills(self, organization, cutoff_date, limit, dry_run):
        """Process analyzed expense bills for an organization."""
        self.stdout.write(f'\n  💰 Processing Expense Bills...')

        # Get analyzed expense bills
        queryset = ExpenseBill.objects.filter(
            organization=organization,
            status='Analysed',
            updated_at__gte=cutoff_date
        ).order_by('-updated_at')

        if limit:
            queryset = queryset[:limit]

        bills = list(queryset)
        self.stdout.write(f'    Found {len(bills)} analyzed expense bills')

        processed = 0
        errors = 0

        for bill in bills:
            try:
                if dry_run:
                    self.stdout.write(f'    [DRY RUN] Would process expense bill ID: {bill.id}')
                    processed += 1
                    continue

                with transaction.atomic():
                    # Check if ExpenseZohoBill already exists
                    try:
                        zoho_bill = ExpenseZohoBill.objects.get(
                            selectBill=bill,
                            organization=organization
                        )
                        self.stdout.write(f'    ✓ Expense bill {bill.id} already has Zoho data')
                        processed += 1
                        continue
                    except ExpenseZohoBill.DoesNotExist:
                        pass

                    # Process analyzed data if available
                    if not bill.analysed_data:
                        self.stdout.write(f'    ⚠️  Expense bill {bill.id} has no analyzed data')
                        continue

                    # Create Zoho objects from analyzed data
                    self.create_expense_zoho_objects(bill, bill.analysed_data, organization)

                    self.stdout.write(f'    ✓ Processed expense bill ID: {bill.id}')
                    processed += 1

            except Exception as e:
                errors += 1
                self.stdout.write(
                    self.style.ERROR(f'    ✗ Error processing expense bill {bill.id}: {str(e)}')
                )
                logger.error(f'Error processing expense bill {bill.id}: {str(e)}', exc_info=True)

        return processed, errors

    def create_vendor_zoho_objects(self, bill, analyzed_data, organization):
        """Create VendorZohoBill and VendorZohoProduct objects from analyzed data."""

        # Process analyzed data based on schema format
        if "properties" in analyzed_data:
            relevant_data = {
                "invoiceNumber": analyzed_data["properties"]["invoiceNumber"]["const"],
                "dateIssued": analyzed_data["properties"]["dateIssued"]["const"],
                "from": analyzed_data["properties"]["from"]["properties"],
                "items": [{"description": item["description"]["const"], "quantity": item["quantity"]["const"],
                           "price": item["price"]["const"]} for item in analyzed_data["properties"]["items"]["items"]],
                "total": analyzed_data["properties"]["total"]["const"],
                "igst": analyzed_data["properties"]["igst"]["const"],
                "cgst": analyzed_data["properties"]["cgst"]["const"],
                "sgst": analyzed_data["properties"]["sgst"]["const"],
            }
        else:
            relevant_data = analyzed_data

        # Try to find vendor by company name
        vendor = None
        company_name = relevant_data.get('from', {}).get('name', '').strip().lower()
        if company_name:
            vendor = ZohoVendor.objects.filter(
                organization=organization,
                companyName__icontains=company_name
            ).first()

        # Parse date
        bill_date = None
        date_issued = relevant_data.get('dateIssued', '')
        if date_issued:
            try:
                bill_date = datetime.strptime(date_issued, '%Y-%m-%d').date()
            except (ValueError, TypeError):
                pass

        # Safe numeric conversion
        def safe_numeric_string(value, default='0'):
            try:
                if value is None:
                    return default
                if isinstance(value, (int, float)):
                    return str(value)
                float(str(value))  # Validate it's numeric
                return str(value)
            except (ValueError, TypeError):
                return default

        # Create VendorZohoBill
        zoho_bill = VendorZohoBill.objects.create(
            selectBill=bill,
            organization=organization,
            vendor=vendor,
            bill_no=relevant_data.get('invoiceNumber', ''),
            bill_date=bill_date,
            total=safe_numeric_string(relevant_data.get('total')),
            igst=safe_numeric_string(relevant_data.get('igst')),
            cgst=safe_numeric_string(relevant_data.get('cgst')),
            sgst=safe_numeric_string(relevant_data.get('sgst')),
            note=f"Processed by command for {company_name or 'Unknown Vendor'}"
        )

        # Create VendorZohoProduct objects
        items = relevant_data.get('items', [])
        for idx, item in enumerate(items):
            try:
                rate = float(item.get('price', 0) or 0)
                quantity = int(item.get('quantity', 0) or 0)
                amount = rate * quantity

                VendorZohoProduct.objects.create(
                    zohoBill=zoho_bill,
                    organization=organization,
                    item_name=item.get('description', f'Item {idx + 1}')[:100],
                    item_details=item.get('description', f'Item {idx + 1}')[:200],
                    rate=str(rate),
                    quantity=str(quantity),
                    amount=str(amount)
                )
            except Exception as e:
                logger.warning(f"Error creating vendor product {idx + 1}: {str(e)}")

        return zoho_bill

    def create_expense_zoho_objects(self, bill, analyzed_data, organization):
        """Create ExpenseZohoBill and ExpenseZohoProduct objects from analyzed data."""

        # Process analyzed data based on schema format
        if "properties" in analyzed_data:
            relevant_data = {
                "invoiceNumber": analyzed_data["properties"]["invoiceNumber"]["const"],
                "dateIssued": analyzed_data["properties"]["dateIssued"]["const"],
                "from": analyzed_data["properties"]["from"]["properties"],
                "items": [{"description": item["description"]["const"], "quantity": item["quantity"]["const"],
                           "price": item["price"]["const"]} for item in analyzed_data["properties"]["items"]["items"]],
                "total": analyzed_data["properties"]["total"]["const"],
                "igst": analyzed_data["properties"]["igst"]["const"],
                "cgst": analyzed_data["properties"]["cgst"]["const"],
                "sgst": analyzed_data["properties"]["sgst"]["const"],
            }
        else:
            relevant_data = analyzed_data

        # Try to find vendor by company name
        vendor = None
        company_name = relevant_data.get('from', {}).get('name', '').strip().lower()
        if company_name:
            vendor = ZohoVendor.objects.filter(
                organization=organization,
                companyName__icontains=company_name
            ).first()

        # Parse date
        bill_date = None
        date_issued = relevant_data.get('dateIssued', '')
        if date_issued:
            try:
                bill_date = datetime.strptime(date_issued, '%Y-%m-%d').date()
            except (ValueError, TypeError):
                pass

        # Safe numeric conversion
        def safe_numeric_string(value, default='0'):
            try:
                if value is None:
                    return default
                if isinstance(value, (int, float)):
                    return str(value)
                float(str(value))  # Validate it's numeric
                return str(value)
            except (ValueError, TypeError):
                return default

        # Create ExpenseZohoBill
        zoho_bill = ExpenseZohoBill.objects.create(
            selectBill=bill,
            organization=organization,
            vendor=vendor,
            bill_no=relevant_data.get('invoiceNumber', ''),
            bill_date=bill_date,
            total=safe_numeric_string(relevant_data.get('total')),
            igst=safe_numeric_string(relevant_data.get('igst')),
            cgst=safe_numeric_string(relevant_data.get('cgst')),
            sgst=safe_numeric_string(relevant_data.get('sgst')),
            note=f"Processed by command for {company_name or 'Unknown Vendor'}"
        )

        # Create ExpenseZohoProduct objects
        items = relevant_data.get('items', [])
        for idx, item in enumerate(items):
            try:
                amount = item.get('price', 0) * item.get('quantity', 1)

                ExpenseZohoProduct.objects.create(
                    expenseZohoBill=zoho_bill,
                    organization=organization,
                    item_details=item.get('description', f'Item {idx + 1}')[:200],
                    amount=safe_numeric_string(amount)
                )
            except Exception as e:
                logger.warning(f"Error creating expense product {idx + 1}: {str(e)}")

        return zoho_bill

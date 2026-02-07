# apps/module/tally/management/commands/populate_ownership_fields.py

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.module.tally.models import TallyVendorBill, TallyExpenseBill


class Command(BaseCommand):
    help = 'Populate ownership fields (bill_belong_your_org, description) for existing bills'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be updated without making changes',
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=100,
            help='Number of records to process in each batch (default: 100)',
        )

    def validate_bill_ownership(self, json_data, organization):
        """Validate if the bill belongs to the organization based on vendor GST number or company name"""
        try:
            # Extract vendor info ("from" field - who sent the bill)
            from_data = json_data.get('from', {})
            if isinstance(from_data, dict):
                vendor_name = from_data.get('name', '').strip()
                vendor_gst = from_data.get('gst_number', '').strip()
            else:
                vendor_name = ''
                vendor_gst = ''

            # Check GST number match first (most accurate)
            if vendor_gst and organization.gst_number:
                if vendor_gst.replace(' ', '').upper() == organization.gst_number.replace(' ', '').upper():
                    return True, f"GST number match: {vendor_gst}"

            # Check organization name match (case-insensitive, partial match)
            if vendor_name and organization.name:
                org_name_clean = organization.name.lower().strip()
                vendor_name_clean = vendor_name.lower().strip()
                
                # Exact match
                if org_name_clean == vendor_name_clean:
                    return True, f"Exact company name match: {vendor_name}"
                
                # Partial match (if organization name is contained in vendor name or vice versa)
                if org_name_clean in vendor_name_clean or vendor_name_clean in org_name_clean:
                    return True, f"Partial company name match: {vendor_name}"
            
            return False, f"No match found. Vendor: {vendor_name}, GST: {vendor_gst}"
            
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Error validating bill ownership: {str(e)}"))
            return False, f"Validation error: {str(e)}"

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        batch_size = options['batch_size']
        
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN MODE - No changes will be made"))
        
        self.stdout.write("Starting ownership fields population...")
        
        # Process Vendor Bills
        self.stdout.write("\\nProcessing TallyVendorBill records...")
        vendor_bills = TallyVendorBill.objects.filter(
            analysed_data__isnull=False
        ).exclude(analysed_data={}).exclude(
            bill_belong_your_org__isnull=False, 
            description__isnull=False
        ).select_related('organization')
        
        vendor_count = vendor_bills.count()
        self.stdout.write(f"Found {vendor_count} vendor bills to process")
        
        updated_vendor_bills = 0
        for i in range(0, vendor_count, batch_size):
            batch = vendor_bills[i:i+batch_size]
            
            with transaction.atomic():
                for bill in batch:
                    if bill.analysed_data and bill.organization:
                        bill_belongs_to_org, ownership_description = self.validate_bill_ownership(
                            bill.analysed_data, bill.organization
                        )
                        
                        if not dry_run:
                            bill.bill_belong_your_org = bill_belongs_to_org
                            bill.description = ownership_description
                            bill.save(update_fields=['bill_belong_your_org', 'description'])
                        
                        status_icon = "✓" if bill_belongs_to_org else "⚬"
                        self.stdout.write(
                            f"  {status_icon} {bill.bill_munshi_name}: {bill_belongs_to_org} - {ownership_description[:70]}..."
                        )
                        updated_vendor_bills += 1
            
            self.stdout.write(f"Processed batch {i//batch_size + 1} ({min(i+batch_size, vendor_count)}/{vendor_count})")
        
        # Process Expense Bills  
        self.stdout.write("\\nProcessing TallyExpenseBill records...")
        expense_bills = TallyExpenseBill.objects.filter(
            analysed_data__isnull=False
        ).exclude(analysed_data={}).exclude(
            bill_belong_your_org__isnull=False, 
            description__isnull=False
        ).select_related('organization')
        
        expense_count = expense_bills.count()
        self.stdout.write(f"Found {expense_count} expense bills to process")
        
        updated_expense_bills = 0
        for i in range(0, expense_count, batch_size):
            batch = expense_bills[i:i+batch_size]
            
            with transaction.atomic():
                for bill in batch:
                    if bill.analysed_data and bill.organization:
                        bill_belongs_to_org, ownership_description = self.validate_bill_ownership(
                            bill.analysed_data, bill.organization
                        )
                        
                        if not dry_run:
                            bill.bill_belong_your_org = bill_belongs_to_org
                            bill.description = ownership_description
                            bill.save(update_fields=['bill_belong_your_org', 'description'])
                        
                        status_icon = "✓" if bill_belongs_to_org else "⚬"
                        self.stdout.write(
                            f"  {status_icon} {bill.bill_munshi_name}: {bill_belongs_to_org} - {ownership_description[:70]}..."
                        )
                        updated_expense_bills += 1
            
            self.stdout.write(f"Processed batch {i//batch_size + 1} ({min(i+batch_size, expense_count)}/{expense_count})")
        
        # Summary
        self.stdout.write("\\n" + "="*60)
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN SUMMARY:"))
            self.stdout.write(f"Would update {updated_vendor_bills} vendor bills")
            self.stdout.write(f"Would update {updated_expense_bills} expense bills")
            self.stdout.write("\\nRun without --dry-run to make actual changes")
        else:
            self.stdout.write(self.style.SUCCESS("COMPLETED SUCCESSFULLY:"))
            self.stdout.write(self.style.SUCCESS(f"Updated {updated_vendor_bills} vendor bills"))
            self.stdout.write(self.style.SUCCESS(f"Updated {updated_expense_bills} expense bills"))
            self.stdout.write(f"Total updated: {updated_vendor_bills + updated_expense_bills} bills")
        
        self.stdout.write("="*60)
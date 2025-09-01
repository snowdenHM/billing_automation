import random
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
import os
import re

from django.core.files import File
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.conf import settings
from django.db import models

from apps.module.tally.models import (
    ParentLedger,
    Ledger,
    TallyConfig,
    TallyVendorBill,
    TallyVendorAnalyzedBill,
    TallyVendorAnalyzedProduct,
    TallyExpenseBill,
    TallyExpenseAnalyzedBill,
    TallyExpenseAnalyzedProduct
)
from apps.organizations.models import Organization


class Command(BaseCommand):
    help = 'Generate dummy data for Tally app models'

    def add_arguments(self, parser):
        parser.add_argument(
            '--org_id',
            type=str,
            help='Organization ID to generate data for',
            required=True
        )
        parser.add_argument(
            '--parent_ledgers',
            type=int,
            help='Number of parent ledgers to create',
            default=10
        )
        parser.add_argument(
            '--ledgers',
            type=int,
            help='Number of ledgers to create per parent',
            default=5
        )
        parser.add_argument(
            '--vendor_bills',
            type=int,
            help='Number of vendor bills to create',
            default=20
        )
        parser.add_argument(
            '--expense_bills',
            type=int,
            help='Number of expense bills to create',
            default=20
        )

    def handle(self, *args, **options):
        try:
            org_id = options['org_id']
            org = Organization.objects.get(id=org_id)
            self.stdout.write(self.style.SUCCESS(f'Found organization: {org.name}'))
        except Organization.DoesNotExist:
            self.stdout.write(self.style.ERROR(f'Organization with ID {org_id} not found'))
            return

        # Create parent ledgers
        parent_ledgers = self._create_parent_ledgers(org, options['parent_ledgers'])

        # Create ledgers for each parent
        ledgers = self._create_ledgers(org, parent_ledgers, options['ledgers'])

        # Create TallyConfig
        self._create_tally_config(org, parent_ledgers)

        # Create vendor bills and analyzed data
        self._create_vendor_bills(org, ledgers, options['vendor_bills'])

        # Create expense bills and analyzed data
        self._create_expense_bills(org, ledgers, options['expense_bills'])

        self.stdout.write(self.style.SUCCESS('Successfully generated dummy data for Tally app'))

    def _create_parent_ledgers(self, org, count):
        parent_ledgers = []
        parent_types = [
            "Current Assets", "Fixed Assets", "Investments", "Current Liabilities",
            "Loans (Liability)", "Capital Account", "Sales Accounts", "Purchase Accounts",
            "Direct Expenses", "Indirect Expenses", "Direct Income", "Indirect Income",
            "IGST", "CGST", "SGST", "Vendors", "Customers"
        ]

        self.stdout.write('Creating parent ledgers...')

        # Ensure we have all important types
        for parent_type in parent_types[:min(count, len(parent_types))]:
            parent = ParentLedger.objects.create(
                organization=org,
                parent=parent_type
            )
            parent_ledgers.append(parent)

        # Add additional random parent ledgers if needed
        for i in range(max(0, count - len(parent_types))):
            parent = ParentLedger.objects.create(
                organization=org,
                parent=f"Custom Parent {i+1}"
            )
            parent_ledgers.append(parent)

        self.stdout.write(self.style.SUCCESS(f'Created {len(parent_ledgers)} parent ledgers'))
        return parent_ledgers

    def _create_ledgers(self, org, parent_ledgers, count_per_parent):
        ledgers = []

        self.stdout.write('Creating ledgers...')

        for parent in parent_ledgers:
            for i in range(count_per_parent):
                ledger = Ledger.objects.create(
                    organization=org,
                    parent=parent,
                    master_id=f"MASTER-{uuid.uuid4().hex[:8]}",
                    alter_id=f"ALTER-{uuid.uuid4().hex[:8]}",
                    name=f"{parent.parent} Ledger {i+1}",
                    alias=f"AL-{parent.parent[:3]}-{i+1}",
                    opening_balance=str(random.randint(10000, 100000)),
                    gst_in=f"27AADCB2230M1Z4" if "Vendor" in parent.parent else None,
                    company=f"Company {parent.parent} {i+1}" if "Vendor" in parent.parent else None
                )
                ledgers.append(ledger)

        self.stdout.write(self.style.SUCCESS(f'Created {len(ledgers)} ledgers'))
        return ledgers

    def _create_tally_config(self, org, parent_ledgers):
        self.stdout.write('Creating Tally configuration...')

        # Find parent ledgers by type
        igst_parents = [p for p in parent_ledgers if "IGST" in p.parent]
        cgst_parents = [p for p in parent_ledgers if "CGST" in p.parent]
        sgst_parents = [p for p in parent_ledgers if "SGST" in p.parent]
        vendor_parents = [p for p in parent_ledgers if "Vendor" in p.parent]
        expense_parents = [p for p in parent_ledgers if "Expense" in p.parent]
        coa_parents = [p for p in parent_ledgers if "Assets" in p.parent or "Liabilities" in p.parent]

        tally_config = TallyConfig.objects.create(organization=org)

        # Add parent ledgers to appropriate categories
        if igst_parents:
            tally_config.igst_parents.add(*igst_parents)
        if cgst_parents:
            tally_config.cgst_parents.add(*cgst_parents)
        if sgst_parents:
            tally_config.sgst_parents.add(*sgst_parents)
        if vendor_parents:
            tally_config.vendor_parents.add(*vendor_parents)
        if coa_parents:
            tally_config.chart_of_accounts_parents.add(*coa_parents)
        if expense_parents:
            tally_config.chart_of_accounts_expense_parents.add(*expense_parents)

        self.stdout.write(self.style.SUCCESS('Created Tally configuration'))
        return tally_config

    def _create_dummy_file(self, prefix):
        """Create a dummy PDF file for testing uploads"""
        dummy_file_path = os.path.join(settings.MEDIA_ROOT, f"dummy_{prefix}_{uuid.uuid4().hex[:8]}.pdf")
        os.makedirs(os.path.dirname(dummy_file_path), exist_ok=True)

        # Create a simple dummy PDF file
        with open(dummy_file_path, 'w') as f:
            f.write(f"%PDF-1.7\n1 0 obj\n<</Type/Catalog/Pages 2 0 R>>\nendobj\n2 0 obj\n<</Type/Pages/Count 1/Kids[3 0 R]>>\nendobj\n3 0 obj\n<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R/Resources<<>>/Contents 4 0 R>>\nendobj\n4 0 obj\n<</Length 15>>\nstream\nBT /F1 12 Tf 100 700 Td (Dummy {prefix} Bill) Tj ET\nendstream\nendobj\nxref\n0 5\n0000000000 65535 f\n0000000010 00000 n\n0000000053 00000 n\n0000000102 00000 n\n0000000182 00000 n\ntrailer\n<</Size 5/Root 1 0 R>>\nstartxref\n245\n%%EOF")

        return dummy_file_path

    def _create_vendor_bills(self, org, ledgers, count):
        self.stdout.write('Creating vendor bills...')

        # Filter ledgers to find vendor ledgers
        vendor_ledgers = [l for l in ledgers if "Vendor" in l.parent.parent]
        if not vendor_ledgers:
            vendor_ledgers = ledgers[:min(len(ledgers), 5)]  # Use first 5 ledgers if no vendors found

        # Tax ledgers
        igst_ledgers = [l for l in ledgers if "IGST" in l.parent.parent]
        cgst_ledgers = [l for l in ledgers if "CGST" in l.parent.parent]
        sgst_ledgers = [l for l in ledgers if "SGST" in l.parent.parent]

        if not igst_ledgers:
            igst_ledgers = [ledgers[0]] if ledgers else []
        if not cgst_ledgers:
            cgst_ledgers = [ledgers[1]] if len(ledgers) > 1 else igst_ledgers
        if not sgst_ledgers:
            sgst_ledgers = [ledgers[2]] if len(ledgers) > 2 else cgst_ledgers

        gst_types = ['IGST', 'CGST_SGST']
        bill_statuses = ['Draft', 'Analysed', 'Verified', 'Synced']

        for i in range(count):
            # Create dummy file
            dummy_file_path = self._create_dummy_file(f"vendor_{i+1}")

            # Determine status - distribute across statuses
            status = bill_statuses[i % len(bill_statuses)]

            try:
                # Create vendor bill - directly use save() to bypass the second save() method with full_clean()
                with open(dummy_file_path, 'rb') as f:
                    bill = TallyVendorBill(
                        organization=org,
                        file=File(f, name=os.path.basename(dummy_file_path)),
                        fileType=random.choice(['Single Invoice/File', 'Multiple Invoice/File']),
                        status=status,
                        process=(status != 'Draft')
                    )
                    # Generate billmunshiName
                    if not bill.billmunshiName:
                        last = (
                            TallyVendorBill.objects.filter(
                                organization=org, billmunshiName__startswith="BM-TB-"
                            )
                            .order_by("-billmunshiName")
                            .first()
                        )
                        if last and last.billmunshiName:
                            m = re.match(r"BM-TB-(\d+)$", last.billmunshiName)
                            next_num = int(m.group(1)) + 1 if m else 1
                        else:
                            next_num = 1
                        bill.billmunshiName = f"BM-TB-{next_num}"
                    # Use the parent's save method to bypass full_clean
                    models.Model.save(bill)

                # If status is beyond Draft, create analyzed data
                if status != 'Draft':
                    gst_type = random.choice(gst_types)
                    vendor = random.choice(vendor_ledgers)
                    total = Decimal(str(random.uniform(1000, 10000))).quantize(Decimal('0.01'))

                    # Generate product data first to calculate proper GST totals
                    product_count = random.randint(1, 3)
                    products_data = []

                    product_igst_sum = Decimal('0.00')
                    product_cgst_sum = Decimal('0.00')
                    product_sgst_sum = Decimal('0.00')

                    for j in range(product_count):
                        product_price = Decimal(str(random.uniform(100, 2000))).quantize(Decimal('0.01'))
                        product_qty = random.randint(1, 5)
                        product_amount = Decimal(str(product_price * product_qty)).quantize(Decimal('0.01'))

                        # Product GST
                        gst_percent = random.choice(['5%', '12%', '18%', '28%'])

                        if gst_type == 'IGST':
                            if gst_percent == '5%':
                                product_igst = Decimal(str(product_amount * Decimal('0.05'))).quantize(Decimal('0.01'))
                            elif gst_percent == '12%':
                                product_igst = Decimal(str(product_amount * Decimal('0.12'))).quantize(Decimal('0.01'))
                            elif gst_percent == '18%':
                                product_igst = Decimal(str(product_amount * Decimal('0.18'))).quantize(Decimal('0.01'))
                            else:  # 28%
                                product_igst = Decimal(str(product_amount * Decimal('0.28'))).quantize(Decimal('0.01'))
                            product_cgst = Decimal('0')
                            product_sgst = Decimal('0')

                            product_igst_sum += product_igst
                        else:  # CGST_SGST
                            product_igst = Decimal('0')
                            if gst_percent == '5%':
                                gst_half = Decimal(str(product_amount * Decimal('0.025'))).quantize(Decimal('0.01'))
                            elif gst_percent == '12%':
                                gst_half = Decimal(str(product_amount * Decimal('0.06'))).quantize(Decimal('0.01'))
                            elif gst_percent == '18%':
                                gst_half = Decimal(str(product_amount * Decimal('0.09'))).quantize(Decimal('0.01'))
                            else:  # 28%
                                gst_half = Decimal(str(product_amount * Decimal('0.14'))).quantize(Decimal('0.01'))
                            product_cgst = gst_half
                            product_sgst = gst_half

                            product_cgst_sum += product_cgst
                            product_sgst_sum += product_sgst

                        products_data.append({
                            'item_name': f"Product {j+1}",
                            'item_details': f"Detailed description for product {j+1}",
                            'taxes': random.choice(igst_ledgers) if igst_ledgers else None,
                            'price': product_price,
                            'quantity': product_qty,
                            'amount': product_amount,
                            'product_gst': gst_percent,
                            'igst': product_igst,
                            'cgst': product_cgst,
                            'sgst': product_sgst
                        })

                    # Calculate GST based on type - use the sum from products
                    if gst_type == 'IGST':
                        igst = product_igst_sum
                        cgst = Decimal('0')
                        sgst = Decimal('0')
                        igst_tax = random.choice(igst_ledgers) if igst_ledgers else None
                        cgst_tax = None
                        sgst_tax = None
                    else:  # CGST_SGST
                        igst = Decimal('0')
                        cgst = product_cgst_sum
                        sgst = product_sgst_sum
                        igst_tax = None
                        cgst_tax = random.choice(cgst_ledgers) if cgst_ledgers else None
                        sgst_tax = random.choice(sgst_ledgers) if sgst_ledgers else None

                    # Now create the analyzed bill header with the correct GST totals
                    try:
                        analyzed_bill = TallyVendorAnalyzedBill(
                            organization=org,
                            selectBill=bill,
                            vendor=vendor,
                            bill_no=f"INV-{i+1000}",
                            bill_date=timezone.now().date() - timedelta(days=random.randint(1, 30)),
                            total=total,
                            igst=igst,
                            cgst=cgst,
                            sgst=sgst,
                            igst_taxes=igst_tax,
                            cgst_taxes=cgst_tax,
                            sgst_taxes=sgst_tax,
                            gst_type=gst_type,
                            note=f"Vendor bill {i+1} analysis"
                        )
                        # Use model's save to bypass validation for now
                        models.Model.save(analyzed_bill)

                        # Now create the products
                        for product_data in products_data:
                            TallyVendorAnalyzedProduct.objects.create(
                                organization=org,
                                vendor_bill_analyzed=analyzed_bill,
                                **product_data
                            )
                    except Exception as e:
                        self.stdout.write(self.style.ERROR(f"Error creating analyzed bill for vendor bill {i+1}: {str(e)}"))

            except Exception as e:
                self.stdout.write(self.style.ERROR(f"Error creating vendor bill {i+1}: {str(e)}"))

            # Clean up dummy file
            if os.path.exists(dummy_file_path):
                os.remove(dummy_file_path)

        self.stdout.write(self.style.SUCCESS(f'Created {count} vendor bills'))

    def _create_expense_bills(self, org, ledgers, count):
        self.stdout.write('Creating expense bills...')

        # Filter ledgers to find vendor ledgers and chart of accounts
        vendor_ledgers = [l for l in ledgers if "Vendor" in l.parent.parent]
        if not vendor_ledgers:
            vendor_ledgers = ledgers[:min(len(ledgers), 5)]  # Use first 5 ledgers if no vendors found

        coa_ledgers = [l for l in ledgers if "Assets" in l.parent.parent or "Expenses" in l.parent.parent]
        if not coa_ledgers:
            coa_ledgers = ledgers[:min(len(ledgers), 5)]  # Use first 5 ledgers if no COA found

        # Tax ledgers
        igst_ledgers = [l for l in ledgers if "IGST" in l.parent.parent]
        cgst_ledgers = [l for l in ledgers if "CGST" in l.parent.parent]
        sgst_ledgers = [l for l in ledgers if "SGST" in l.parent.parent]

        if not igst_ledgers:
            igst_ledgers = [ledgers[0]] if ledgers else []
        if not cgst_ledgers:
            cgst_ledgers = [ledgers[1]] if len(ledgers) > 1 else igst_ledgers
        if not sgst_ledgers:
            sgst_ledgers = [ledgers[2]] if len(ledgers) > 2 else cgst_ledgers

        bill_statuses = ['Draft', 'Analysed', 'Verified', 'Synced']
        debit_credit = ['debit', 'credit']

        for i in range(count):
            # Create dummy file
            dummy_file_path = self._create_dummy_file(f"expense_{i+1}")

            # Determine status - distribute across statuses
            status = bill_statuses[i % len(bill_statuses)]

            try:
                # Create expense bill - use the same approach as for vendor bills
                with open(dummy_file_path, 'rb') as f:
                    bill = TallyExpenseBill(
                        organization=org,
                        file=File(f, name=os.path.basename(dummy_file_path)),
                        fileType=random.choice(['Single Invoice/File', 'Multiple Invoice/File']),
                        status=status,
                        process=(status != 'Draft')
                    )
                    # Generate billmunshiName
                    if not bill.billmunshiName:
                        last = (
                            TallyExpenseBill.objects.filter(
                                organization=org, billmunshiName__startswith="BM-TE-"
                            )
                            .order_by("-billmunshiName")
                            .first()
                        )
                        if last and last.billmunshiName:
                            m = re.match(r"BM-TE-(\d+)$", last.billmunshiName)
                            next_num = int(m.group(1)) + 1 if m else 1
                        else:
                            next_num = 1
                        bill.billmunshiName = f"BM-TE-{next_num}"
                    # Use the parent's save method to bypass full_clean
                    models.Model.save(bill)

                # If status is beyond Draft, create analyzed data
                if status != 'Draft':
                    vendor = random.choice(vendor_ledgers)
                    total = Decimal(str(random.uniform(1000, 10000))).quantize(Decimal('0.01'))

                    # Create analyzed bill header
                    analyzed_bill = TallyExpenseAnalyzedBill(
                        organization=org,
                        selectBill=bill,
                        vendor=vendor,
                        voucher=f"VOUCHER-{i+1000}",
                        bill_no=f"EXP-{i+1000}",
                        bill_date=timezone.now().date() - timedelta(days=random.randint(1, 30)),
                        total=total,
                        igst=Decimal('0'),
                        cgst=Decimal('0'),
                        sgst=Decimal('0'),
                        igst_taxes=random.choice(igst_ledgers) if igst_ledgers else None,
                        cgst_taxes=random.choice(cgst_ledgers) if cgst_ledgers else None,
                        sgst_taxes=random.choice(sgst_ledgers) if sgst_ledgers else None,
                        note=f"Expense bill {i+1} analysis"
                    )
                    # Use Django's model save to bypass any custom save method
                    models.Model.save(analyzed_bill)

                    # Create 1-3 expense entries for this bill
                    entry_count = random.randint(1, 3)
                    for j in range(entry_count):
                        entry_amount = Decimal(str(total / entry_count)).quantize(Decimal('0.01'))

                        TallyExpenseAnalyzedProduct.objects.create(
                            organization=org,
                            expense_bill=analyzed_bill,
                            item_details=f"Expense entry {j+1}",
                            chart_of_accounts=random.choice(coa_ledgers),
                            amount=entry_amount,
                            debit_or_credit=random.choice(debit_credit)
                        )
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"Error creating expense bill {i+1}: {str(e)}"))

            # Clean up dummy file
            if os.path.exists(dummy_file_path):
                os.remove(dummy_file_path)

        self.stdout.write(self.style.SUCCESS(f'Created {count} expense bills'))

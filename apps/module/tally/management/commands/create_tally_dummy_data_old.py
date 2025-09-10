import os
import uuid
from decimal import Decimal
from datetime import date, datetime, timedelta
from django.core.management.base import BaseCommand
from django.core.files.base import ContentFile
from django.db import transaction

from apps.organizations.models import Organization
from apps.module.tally.models import (
    ParentLedger, Ledger, TallyConfig,
    TallyVendorBill, TallyVendorAnalyzedBill, TallyVendorAnalyzedProduct,
    TallyExpenseBill, TallyExpenseAnalyzedBill, TallyExpenseAnalyzedProduct
)


class Command(BaseCommand):
    help = 'Insert comprehensive dummy data for Tally app testing'

    def add_arguments(self, parser):
        parser.add_argument(
            '--org-id',
            type=str,
            help='Organization UUID to create dummy data for',
        )
        parser.add_argument(
            '--clear-existing',
            action='store_true',
            help='Clear existing tally data before creating new data',
        )

    def handle(self, *args, **options):
        org_id = options.get('org_id')
        clear_existing = options.get('clear_existing', False)

        if org_id:
            try:
                organization = Organization.objects.get(id=org_id)
            except Organization.DoesNotExist:
                self.stdout.write(
                    self.style.ERROR(f'Organization with ID {org_id} not found')
                )
                return
        else:
            # Get first organization or create one
            organization = Organization.objects.first()
            if not organization:
                organization = Organization.objects.create(
                    name="Test Organization",
                    description="Organization for Tally testing"
                )
                self.stdout.write(
                    self.style.SUCCESS(f'Created test organization: {organization.name}')
                )

        if clear_existing:
            self.clear_existing_data(organization)

        try:
            with transaction.atomic():
                self.create_dummy_data(organization)
                self.stdout.write(
                    self.style.SUCCESS(
                        f'Successfully created dummy data for organization: {organization.name}'
                    )
                )
        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f'Error creating dummy data: {str(e)}')
            )
            import traceback
            self.stdout.write(self.style.ERROR(traceback.format_exc()))

    def clear_existing_data(self, organization):
        """Clear existing tally data for the organization"""
        self.stdout.write('Clearing existing tally data...')

        # Clear in reverse dependency order
        TallyVendorAnalyzedProduct.objects.filter(organization=organization).delete()
        TallyVendorAnalyzedBill.objects.filter(organization=organization).delete()
        TallyVendorBill.objects.filter(organization=organization).delete()

        TallyExpenseAnalyzedProduct.objects.filter(organization=organization).delete()
        TallyExpenseAnalyzedBill.objects.filter(organization=organization).delete()
        TallyExpenseBill.objects.filter(organization=organization).delete()

        TallyConfig.objects.filter(organization=organization).delete()
        Ledger.objects.filter(organization=organization).delete()
        ParentLedger.objects.filter(organization=organization).delete()

        self.stdout.write(self.style.SUCCESS('Cleared existing data'))

    def create_dummy_data(self, organization):
        """Create comprehensive dummy data for testing"""
        self.stdout.write('Creating dummy data...')

        # Create Parent Ledgers
        parent_ledgers = self.create_parent_ledgers(organization)

        # Create Ledgers
        ledgers = self.create_ledgers(organization, parent_ledgers)

        # Create Tally Config
        tally_config = self.create_tally_config(organization, parent_ledgers)

        # Create Vendor Bills
        vendor_bills = self.create_vendor_bills(organization)

        # Create Analyzed Vendor Bills
        self.create_analyzed_vendor_bills(organization, vendor_bills, ledgers)

        # Create Expense Bills
        expense_bills = self.create_expense_bills(organization)

        # Create Analyzed Expense Bills
        self.create_analyzed_expense_bills(organization, expense_bills, ledgers)

    def create_parent_ledgers(self, organization):
        """Create parent ledgers for different categories"""
        parent_ledger_names = [
            "Sundry Creditors",
            "Sundry Debtors",
            "Duties & Taxes",
            "Current Assets",
            "Current Liabilities",
            "Direct Expenses",
            "Indirect Expenses",
            "Sales Accounts",
            "Purchase Accounts",
            "Bank Accounts",
            "Cash-in-Hand"
        ]

        parent_ledgers = {}
        for name in parent_ledger_names:
            parent_ledger = ParentLedger.objects.create(
                parent=name,
                organization=organization
            )
            parent_ledgers[name] = parent_ledger

        self.stdout.write(f'Created {len(parent_ledgers)} parent ledgers')
        return parent_ledgers

    def create_ledgers(self, organization, parent_ledgers):
        """Create ledgers under parent ledgers"""
        ledgers = {}

        # Vendor ledgers under Sundry Creditors
        vendor_data = [
            {"name": "ABC Suppliers Pvt Ltd", "master_id": "VEN001", "gst": "27AABCS1234A1ZN"},
            {"name": "XYZ Technologies", "master_id": "VEN002", "gst": "19XYZAB1234B1ZN"},
            {"name": "Quick Services Ltd", "master_id": "VEN003", "gst": "29QSLAB1234C1ZN"},
            {"name": "Global Traders", "master_id": "VEN004", "gst": "06GTRAB1234D1ZN"},
            {"name": "Metro Solutions", "master_id": "VEN005", "gst": "07MSOAB1234E1ZN"},
            {"name": "Prime Vendors", "master_id": "VEN006", "gst": "24PVDAB1234F1ZN"},
            {"name": "Elite Suppliers", "master_id": "VEN007", "gst": "33ELSAB1234G1ZN"},
            {"name": "Best Buy Corporation", "master_id": "VEN008", "gst": "09BBCAB1234H1ZN"},
            {"name": "Supreme Trading Co", "master_id": "VEN009", "gst": "36STCAB1234I1ZN"},
            {"name": "Modern Enterprises", "master_id": "VEN010", "gst": "22MELAB1234J1ZN"},
        ]

        for vendor in vendor_data:
            ledger = Ledger.objects.create(
                master_id=vendor["master_id"],
                name=vendor["name"],
                parent=parent_ledgers["Sundry Creditors"],
                gst_in=vendor["gst"],
                company=vendor["name"],
                opening_balance=Decimal("50000.00"),
                organization=organization
            )
            ledgers[f"vendor_{vendor['name'].lower().replace(' ', '_')}"] = ledger

        # Tax ledgers under Duties & Taxes
        tax_ledgers = [
            {"name": "IGST @ 18%", "key": "igst_18", "master_id": "TAX001"},
            {"name": "CGST @ 9%", "key": "cgst_9", "master_id": "TAX002"},
            {"name": "SGST @ 9%", "key": "sgst_9", "master_id": "TAX003"},
            {"name": "IGST @ 12%", "key": "igst_12", "master_id": "TAX004"},
            {"name": "CGST @ 6%", "key": "cgst_6", "master_id": "TAX005"},
            {"name": "SGST @ 6%", "key": "sgst_6", "master_id": "TAX006"},
            {"name": "IGST @ 5%", "key": "igst_5", "master_id": "TAX007"},
            {"name": "CGST @ 2.5%", "key": "cgst_2_5", "master_id": "TAX008"},
            {"name": "SGST @ 2.5%", "key": "sgst_2_5", "master_id": "TAX009"},
        ]

        for tax_ledger in tax_ledgers:
            ledger = Ledger.objects.create(
                master_id=tax_ledger["master_id"],
                name=tax_ledger["name"],
                parent=parent_ledgers["Duties & Taxes"],
                organization=organization
            )
            ledgers[tax_ledger["key"]] = ledger

        # Expense ledgers
        expense_data = [
            {"name": "Office Rent", "master_id": "EXP001"},
            {"name": "Electricity Expenses", "master_id": "EXP002"},
            {"name": "Telephone Expenses", "master_id": "EXP003"},
            {"name": "Internet Charges", "master_id": "EXP004"},
            {"name": "Stationery Expenses", "master_id": "EXP005"},
            {"name": "Printing & Stationery", "master_id": "EXP006"},
            {"name": "Travel Expenses", "master_id": "EXP007"},
            {"name": "Fuel Expenses", "master_id": "EXP008"},
            {"name": "Maintenance Expenses", "master_id": "EXP009"},
            {"name": "Professional Fees", "master_id": "EXP010"},
            {"name": "Legal & Professional Charges", "master_id": "EXP011"},
            {"name": "Audit Fees", "master_id": "EXP012"},
            {"name": "Software Licenses", "master_id": "EXP013"},
            {"name": "Computer Hardware", "master_id": "EXP014"},
            {"name": "Office Equipment", "master_id": "EXP015"},
            {"name": "Marketing Expenses", "master_id": "EXP016"},
            {"name": "Advertising Expenses", "master_id": "EXP017"},
            {"name": "Website Maintenance", "master_id": "EXP018"},
        ]

        for expense in expense_data:
            ledger = Ledger.objects.create(
                master_id=expense["master_id"],
                name=expense["name"],
                parent=parent_ledgers["Direct Expenses"],
                organization=organization
            )
            ledgers[f"expense_{expense['name'].lower().replace(' ', '_').replace('&', 'and')}"] = ledger

        self.stdout.write(f'Created {len(ledgers)} ledgers')
        return ledgers

    def create_tally_config(self, organization, parent_ledgers):
        """Create tally configuration"""
        config = TallyConfig.objects.create(organization=organization)

        # Add parent ledgers to different categories
        config.igst_parents.add(parent_ledgers["Duties & Taxes"])
        config.cgst_parents.add(parent_ledgers["Duties & Taxes"])
        config.sgst_parents.add(parent_ledgers["Duties & Taxes"])
        config.vendor_parents.add(parent_ledgers["Sundry Creditors"])
        config.chart_of_accounts_parents.add(parent_ledgers["Current Assets"])
        config.chart_of_accounts_parents.add(parent_ledgers["Current Liabilities"])
        config.chart_of_accounts_expense_parents.add(parent_ledgers["Direct Expenses"])
        config.chart_of_accounts_expense_parents.add(parent_ledgers["Indirect Expenses"])

        self.stdout.write('Created tally configuration')
        return config

    def create_vendor_bills(self, organization):
        """Create vendor bills in different stages"""
        vendor_bills = []

        # Create bills with predefined data
        bill_data = [
            {"status": "Draft", "type": "Single Invoice/File", "bill_no": "VB001"},
            {"status": "Draft", "type": "Single Invoice/File", "bill_no": "VB002"},
            {"status": "Draft", "type": "Multiple Invoice/File", "bill_no": "VB003"},
            {"status": "Analysed", "type": "Single Invoice/File", "bill_no": "VB004"},
            {"status": "Analysed", "type": "Single Invoice/File", "bill_no": "VB005"},
            {"status": "Analysed", "type": "Single Invoice/File", "bill_no": "VB006"},
            {"status": "Analysed", "type": "Multiple Invoice/File", "bill_no": "VB007"},
            {"status": "Verified", "type": "Single Invoice/File", "bill_no": "VB008"},
            {"status": "Verified", "type": "Single Invoice/File", "bill_no": "VB009"},
            {"status": "Verified", "type": "Single Invoice/File", "bill_no": "VB010"},
            {"status": "Verified", "type": "Multiple Invoice/File", "bill_no": "VB011"},
            {"status": "Synced", "type": "Single Invoice/File", "bill_no": "VB012"},
            {"status": "Synced", "type": "Single Invoice/File", "bill_no": "VB013"},
            {"status": "Synced", "type": "Single Invoice/File", "bill_no": "VB014"},
            {"status": "Synced", "type": "Multiple Invoice/File", "bill_no": "VB015"},
            {"status": "Draft", "type": "Single Invoice/File", "bill_no": "VB016"},
            {"status": "Analysed", "type": "Single Invoice/File", "bill_no": "VB017"},
            {"status": "Verified", "type": "Single Invoice/File", "bill_no": "VB018"},
            {"status": "Synced", "type": "Single Invoice/File", "bill_no": "VB019"},
            {"status": "Synced", "type": "Multiple Invoice/File", "bill_no": "VB020"},
        ]

        for i, data in enumerate(bill_data):
            # Create dummy PDF content
            pdf_content = b"%PDF-1.4 Dummy Vendor Bill Content"

            bill = TallyVendorBill.objects.create(
                file=ContentFile(pdf_content, name=f"dummy_vendor_{i+1}.pdf"),
                file_type=data["type"],
                status=data["status"],
                process=data["status"] != "Draft",
                organization=organization,
                analysed_data={
                    "billNumber": data["bill_no"],
                    "dateIssued": "2025-08-15",
                    "vendor": {
                        "name": "ABC Suppliers Pvt Ltd",
                        "address": "123 Business Street, Mumbai, Maharashtra 400001"
                    },
                    "products": [
                        {
                            "name": "Software License",
                            "quantity": 1,
                            "price": 10000.0,
                            "amount": 10000.0
                        }
                    ],
                    "total": 11800.0,
                    "igst": 1800.0,
                    "cgst": 0.0,
                    "sgst": 0.0
                }
            )
            vendor_bills.append(bill)

        self.stdout.write(f'Created {len(vendor_bills)} vendor bills')
        return vendor_bills

    def create_analyzed_vendor_bills(self, organization, vendor_bills, ledgers):
        """Create analyzed vendor bills for bills that are analyzed or beyond"""
        analyzed_bills = []

        # Get vendor and tax ledgers
        vendor_ledgers = [ledger for key, ledger in ledgers.items() if key.startswith('vendor_')]
        igst_ledgers = [ledger for key, ledger in ledgers.items() if 'igst' in key]
        cgst_ledgers = [ledger for key, ledger in ledgers.items() if 'cgst' in key]
        sgst_ledgers = [ledger for key, ledger in ledgers.items() if 'sgst' in key]

        for i, bill in enumerate(vendor_bills):
            if bill.status in ["Analysed", "Verified", "Synced"]:
                # Predefined GST scenarios
                if i % 2 == 0:  # IGST scenario
                    gst_type = TallyVendorAnalyzedBill.GSTType.IGST
                    base_amount = Decimal("10000.00")
                    igst_amount = Decimal("1800.00")  # 18%
                    cgst_amount = Decimal("0.00")
                    sgst_amount = Decimal("0.00")
                else:  # CGST+SGST scenario
                    gst_type = TallyVendorAnalyzedBill.GSTType.CGST_SGST
                    base_amount = Decimal("10000.00")
                    igst_amount = Decimal("0.00")
                    cgst_amount = Decimal("900.00")  # 9%
                    sgst_amount = Decimal("900.00")  # 9%

                total_amount = base_amount + igst_amount + cgst_amount + sgst_amount

                # Create analyzed bill with zero GST amounts initially to avoid validation error
                analyzed_bill = TallyVendorAnalyzedBill.objects.create(
                    selected_bill=bill,
                    vendor=vendor_ledgers[i % len(vendor_ledgers)],
                    bill_no=f"VB-{str(i+1).zfill(6)}",
                    bill_date=date(2025, 8, 15),
                    total=base_amount,  # Start with base amount only
                    igst=Decimal("0.00"),  # Start with zero
                    igst_taxes=igst_ledgers[0] if igst_amount > 0 else None,
                    cgst=Decimal("0.00"),  # Start with zero
                    cgst_taxes=cgst_ledgers[0] if cgst_amount > 0 else None,
                    sgst=Decimal("0.00"),  # Start with zero
                    sgst_taxes=sgt_ledgers[0] if sgst_amount > 0 else None,
                    gst_type=gst_type,
                    note="Sample vendor bill for testing",
                    organization=organization
                )

                # Create products that match the GST calculations exactly
                product_count = 2
                for j in range(product_count):
                    product_amount = base_amount / product_count

                    if gst_type == TallyVendorAnalyzedBill.GSTType.IGST:
                        product_igst = igst_amount / product_count
                        product_cgst = Decimal('0')
                        product_sgst = Decimal('0')
                    else:
                        product_igst = Decimal('0')
                        product_cgst = cgst_amount / product_count
                        product_sgst = sgst_amount / product_count

                    TallyVendorAnalyzedProduct.objects.create(
                        vendor_bill_analyzed=analyzed_bill,
                        item_name=f"Product {j+1}",
                        item_details=f"Sample product {j+1} for testing",
                        price=product_amount,
                        quantity=1,
                        amount=product_amount,
                        product_gst="18%",
                        igst=product_igst,
                        cgst=product_cgst,
                        sgst=product_sgst,
                        organization=organization
                    )

                # Now update the analyzed bill with correct GST amounts
                analyzed_bill.igst = igst_amount
                analyzed_bill.cgst = cgst_amount
                analyzed_bill.sgst = sgst_amount
                analyzed_bill.total = total_amount
                analyzed_bill.save(update_fields=['igst', 'cgst', 'sgst', 'total'])

                analyzed_bills.append(analyzed_bill)

        self.stdout.write(f'Created {len(analyzed_bills)} analyzed vendor bills with products')

    def create_expense_bills(self, organization):
        """Create expense bills in different stages"""
        expense_bills = []

        # Create bills with predefined data
        bill_data = [
            {"status": "Draft", "type": "Single Invoice/File", "bill_no": "EXP001"},
            {"status": "Draft", "type": "Single Invoice/File", "bill_no": "EXP002"},
            {"status": "Draft", "type": "Multiple Invoice/File", "bill_no": "EXP003"},
            {"status": "Analysed", "type": "Single Invoice/File", "bill_no": "EXP004"},
            {"status": "Analysed", "type": "Single Invoice/File", "bill_no": "EXP005"},
            {"status": "Analysed", "type": "Multiple Invoice/File", "bill_no": "EXP006"},
            {"status": "Verified", "type": "Single Invoice/File", "bill_no": "EXP007"},
            {"status": "Verified", "type": "Single Invoice/File", "bill_no": "EXP008"},
            {"status": "Verified", "type": "Multiple Invoice/File", "bill_no": "EXP009"},
            {"status": "Synced", "type": "Single Invoice/File", "bill_no": "EXP010"},
            {"status": "Synced", "type": "Single Invoice/File", "bill_no": "EXP011"},
            {"status": "Synced", "type": "Multiple Invoice/File", "bill_no": "EXP012"},
            {"status": "Draft", "type": "Single Invoice/File", "bill_no": "EXP013"},
            {"status": "Analysed", "type": "Single Invoice/File", "bill_no": "EXP014"},
            {"status": "Verified", "type": "Single Invoice/File", "bill_no": "EXP015"},
        ]

        for i, data in enumerate(bill_data):
            # Create dummy PDF content
            pdf_content = b"%PDF-1.4 Dummy Expense Bill Content"

            bill = TallyExpenseBill.objects.create(
                file=ContentFile(pdf_content, name=f"dummy_expense_{i+1}.pdf"),
                file_type=data["type"],
                status=data["status"],
                process=data["status"] != "Draft",
                organization=organization,
                analysed_data={
                    "billNumber": data["bill_no"],
                    "dateIssued": "2025-08-15",
                    "from": {
                        "name": "Office Supplies Co",
                        "address": "456 Supply Street, Delhi, Delhi 110001"
                    },
                    "to": {
                        "name": organization.name,
                        "address": "789 Business Avenue, Bangalore, Karnataka 560001"
                    },
                    "expenses": [
                        {
                            "description": "Office rent for August 2025",
                            "category": "Office Rent",
                            "amount": 5000.0
                        }
                    ],
                    "total": 5900.0,
                    "igst": 900.0,
                    "cgst": 0.0,
                    "sgst": 0.0
                }
            )
            expense_bills.append(bill)

        self.stdout.write(f'Created {len(expense_bills)} expense bills')
        return expense_bills

    def create_analyzed_expense_bills(self, organization, expense_bills, ledgers):
        """Create analyzed expense bills for bills that are analyzed or beyond"""
        analyzed_bills = []

        # Get vendor and expense ledgers
        vendor_ledgers = [ledger for key, ledger in ledgers.items() if key.startswith('vendor_')]
        expense_ledgers = [ledger for key, ledger in ledgers.items() if key.startswith('expense_')]
        igst_ledgers = [ledger for key, ledger in ledgers.items() if 'igst' in key]
        cgst_ledgers = [ledger for key, ledger in ledgers.items() if 'cgst' in key]
        sgst_ledgers = [ledger for key, ledger in ledgers.items() if 'sgst' in key]

        for i, bill in enumerate(expense_bills):
            if bill.status in ["Analysed", "Verified", "Synced"]:
                # Predefined GST scenarios for expenses
                if i % 3 == 0:  # IGST scenario
                    base_amount = Decimal("5000.00")
                    igst_amount = Decimal("900.00")  # 18%
                    cgst_amount = Decimal("0.00")
                    sgst_amount = Decimal("0.00")
                    selected_igst_taxes = igst_ledgers[0] if igst_ledgers else None
                    selected_cgst_taxes = None
                    selected_sgst_taxes = None
                elif i % 3 == 1:  # CGST+SGST scenario
                    base_amount = Decimal("5000.00")
                    igst_amount = Decimal("0.00")
                    cgst_amount = Decimal("450.00")  # 9%
                    sgst_amount = Decimal("450.00")  # 9%
                    selected_igst_taxes = None
                    selected_cgst_taxes = cgst_ledgers[0] if cgst_ledgers else None
                    selected_sgst_taxes = sgst_ledgers[0] if sgst_ledgers else None
                else:  # No GST scenario
                    base_amount = Decimal("5000.00")
                    igst_amount = Decimal("0.00")
                    cgst_amount = Decimal("0.00")
                    sgst_amount = Decimal("0.00")
                    selected_igst_taxes = None
                    selected_cgst_taxes = None
                    selected_sgst_taxes = None

                total_amount = base_amount + igst_amount + cgst_amount + sgst_amount

                analyzed_bill = TallyExpenseAnalyzedBill.objects.create(
                    selected_bill=bill,
                    vendor=vendor_ledgers[i % len(vendor_ledgers)],
                    voucher=f"EXP-VOUCHER-{str(i+1).zfill(4)}",
                    bill_no=f"EXP-{str(i+1).zfill(6)}",
                    bill_date=date(2025, 8, 15),
                    total=total_amount,
                    igst=igst_amount,
                    igst_taxes=selected_igst_taxes,
                    cgst=cgst_amount,
                    cgst_taxes=selected_cgst_taxes,
                    sgst=sgt_amount,
                    sgst_taxes=selected_sgst_taxes,
                    note="Sample expense bill for testing",
                    organization=organization
                )

                # Create DEBIT entries (Expenses and GST)
                # 1. Main expense item (DEBIT)
                expense_details = [
                    "Office rent for August 2025",
                    "Software license fees",
                    "Professional consultation",
                    "Equipment maintenance",
                    "Travel expenses"
                ]

                TallyExpenseAnalyzedProduct.objects.create(
                    expense_bill=analyzed_bill,
                    item_details=expense_details[i % len(expense_details)],
                    chart_of_accounts=expense_ledgers[i % len(expense_ledgers)],
                    amount=base_amount,
                    debit_or_credit=TallyExpenseAnalyzedProduct.DebitCredit.DEBIT,
                    organization=organization
                )

                # 2. GST DEBIT entries (Input GST - can be claimed back)
                if igst_amount > 0 and selected_igst_taxes:
                    TallyExpenseAnalyzedProduct.objects.create(
                        expense_bill=analyzed_bill,
                        item_details=f"IGST @ 18% on {expense_details[i % len(expense_details)]}",
                        chart_of_accounts=selected_igst_taxes,
                        amount=igst_amount,
                        debit_or_credit=TallyExpenseAnalyzedProduct.DebitCredit.DEBIT,
                        organization=organization
                    )

                if cgst_amount > 0 and selected_cgst_taxes:
                    TallyExpenseAnalyzedProduct.objects.create(
                        expense_bill=analyzed_bill,
                        item_details=f"CGST @ 9% on {expense_details[i % len(expense_details)]}",
                        chart_of_accounts=selected_cgst_taxes,
                        amount=cgst_amount,
                        debit_or_credit=TallyExpenseAnalyzedProduct.DebitCredit.DEBIT,
                        organization=organization
                    )

                if sgst_amount > 0 and selected_sgst_taxes:
                    TallyExpenseAnalyzedProduct.objects.create(
                        expense_bill=analyzed_bill,
                        item_details=f"SGST @ 9% on {expense_details[i % len(expense_details)]}",
                        chart_of_accounts=selected_sgst_taxes,
                        amount=sgt_amount,
                        debit_or_credit=TallyExpenseAnalyzedProduct.DebitCredit.DEBIT,
                        organization=organization
                    )

                # 3. CREDIT entry for Vendor/Creditor (to balance the accounting equation)
                # Total credit should equal total debit (base_amount + total_gst)
                TallyExpenseAnalyzedProduct.objects.create(
                    expense_bill=analyzed_bill,
                    item_details=f"Amount payable to {vendor_ledgers[i % len(vendor_ledgers)].name}",
                    chart_of_accounts=vendor_ledgers[i % len(vendor_ledgers)],
                    amount=total_amount,
                    debit_or_credit=TallyExpenseAnalyzedProduct.DebitCredit.CREDIT,
                    organization=organization
                )

                analyzed_bills.append(analyzed_bill)

        self.stdout.write(f'Created {len(analyzed_bills)} analyzed expense bills with balanced debit/credit entries')

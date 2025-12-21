#!/usr/bin/env python3
"""
Tally Expense Bills - Consolidation Object Creation Script
Creates consolidated products for existing Tally expense analyzed bills with multiple line items.

Usage: python create_tally_expense_consolidate.py
"""

import os
import django
from decimal import Decimal

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.local')
django.setup()

import django.db.models
from apps.module.tally.models import (
    TallyExpenseAnalyzedBill, TallyExpenseAnalyzedProduct, TallyExpenseConsolidatedProduct
)

def create_consolidated_expense_products():
    """Create consolidated products for Tally expense bills with multiple line items"""
    print("🚀 Starting consolidated product creation for existing Tally expense bills...")
    print("=" * 70)

    # Find all analyzed expense bills with multiple products (2 or more)
    bills_with_multiple_products = TallyExpenseAnalyzedBill.objects.filter(
        products__isnull=False
    ).annotate(
        product_count=django.db.models.Count('products')
    ).filter(
        product_count__gt=1
    ).distinct()

    print(f"📊 Found {bills_with_multiple_products.count()} expense bills with multiple products")
    print("-" * 70)

    created_count = 0
    updated_count = 0
    skipped_count = 0
    error_count = 0

    for idx, analyzed_bill in enumerate(bills_with_multiple_products, 1):
        print(f"🔄 Processing bill {idx}/{bills_with_multiple_products.count()}: {analyzed_bill.bill_no}")

        try:
            # Get all products for this analyzed bill
            products = analyzed_bill.products.all()
            product_count = products.count()

            if product_count <= 1:
                print(f"  ℹ️  Only {product_count} product(s), skipping consolidation")
                skipped_count += 1
                continue

            # Check if consolidated product already exists
            existing_consolidated = TallyExpenseConsolidatedProduct.objects.filter(
                expense_bill=analyzed_bill
            ).first()

            # Calculate consolidated data
            total_amount = sum(Decimal(str(p.amount or 0)) for p in products)

            # Create detailed breakdown
            item_details = []
            for product in products:
                amount = Decimal(str(product.amount or 0))
                debit_credit = getattr(product, 'debit_or_credit', 'debit')
                chart_account = getattr(product, 'chart_of_accounts', None)
                chart_name = str(chart_account) if chart_account else 'No Account'
                item_details.append(
                    f"• {product.item_details} ({debit_credit.title()}: ₹{amount}, Account: {chart_name})"
                )

            consolidated_details = f"Consolidated {product_count} expense entries:\n" + "\n".join(item_details)

            # Determine consolidated debit_or_credit (use most common or default to debit for expenses)
            debit_credits = [getattr(p, 'debit_or_credit', 'debit') for p in products]
            most_common_debit_credit = max(set(debit_credits), key=debit_credits.count) if debit_credits else 'debit'

            if existing_consolidated:
                # Update existing consolidated product
                existing_consolidated.item_details = consolidated_details
                existing_consolidated.amount = total_amount
                existing_consolidated.debit_or_credit = most_common_debit_credit
                existing_consolidated.original_entries_count = product_count
                existing_consolidated.consolidation_notes = f'Updated script run - {product_count} expense entries consolidated'
                existing_consolidated.save()

                print(f"  ✅ Updated existing consolidated product ({most_common_debit_credit.title()}: ₹{total_amount})")
                updated_count += 1
            else:
                # Create new consolidated product
                TallyExpenseConsolidatedProduct.objects.create(
                    expense_bill=analyzed_bill,
                    organization=analyzed_bill.organization,
                    item_details=consolidated_details,
                    amount=total_amount,
                    debit_or_credit=most_common_debit_credit,
                    original_entries_count=product_count,
                    consolidation_notes=f'Script created - {product_count} expense entries consolidated'
                )

                print(f"  ✅ Created new consolidated product ({most_common_debit_credit.title()}: ₹{total_amount})")
                created_count += 1

        except Exception as e:
            print(f"  ❌ Error processing bill {analyzed_bill.bill_no}: {str(e)}")
            error_count += 1
            continue

    print("=" * 70)
    print("📈 SUMMARY:")
    print(f"   Total bills processed: {bills_with_multiple_products.count()}")
    print(f"   ✅ Consolidated products created: {created_count}")
    print(f"   🔄 Consolidated products updated: {updated_count}")
    print(f"   ⏭️  Bills skipped: {skipped_count}")
    print(f"   ❌ Errors encountered: {error_count}")
    print("=" * 70)

    if created_count > 0 or updated_count > 0:
        print("✅ Consolidated products are ready for Tally expense bills!")

        # Ask if user wants to set consolidate=True for bills with consolidated products
        response = input("\n🤔 Do you want to set consolidate=True for bills with consolidated products? (y/N): ")
        if response.lower() in ['y', 'yes']:
            update_consolidate_flags(created_count, updated_count)
    else:
        print("ℹ️  No consolidated products were created or updated")

def update_consolidate_flags(created_count, updated_count):
    """Update consolidate flags for analyzed bills with consolidated products"""
    print("\n🔄 Updating consolidate flags...")

    bills_updated = TallyExpenseAnalyzedBill.objects.filter(
        consolidated_product__isnull=False,
        consolidate=False
    ).update(consolidate=True)

    print(f"✅ Updated {bills_updated} bills with consolidate=True")

if __name__ == "__main__":
    print("🎯 Tally Expense Bills - Consolidated Product Creation")
    print("This script will create consolidated products for analyzed bills with multiple line items.")
    print()

    try:
        create_consolidated_expense_products()
        print("\n🎉 Script completed successfully!")
    except KeyboardInterrupt:
        print("\n⏹️  Script interrupted by user")
    except Exception as e:
        print(f"\n💥 Script failed with error: {str(e)}")
        import traceback
        traceback.print_exc()


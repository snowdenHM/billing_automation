#!/usr/bin/env python3
"""
Zoho Vendor Bills - Consolidation Object Creation Script
Creates consolidated products for existing Zoho vendor bills with multiple line items.

Usage: python create_zoho_vendor_consolidate.py
"""

import os
import django
from decimal import Decimal

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.local')
django.setup()

import django.db.models
from apps.module.zoho.models import (
    VendorZohoBill, VendorZohoProduct, VendorZohoConsolidatedProduct
)

def create_consolidated_vendor_products():
    """Create consolidated products for Zoho vendor bills with multiple line items"""
    print("🚀 Starting consolidated product creation for existing Zoho vendor bills...")
    print("=" * 70)

    # Find all vendor bills with multiple products (2 or more)
    bills_with_multiple_products = VendorZohoBill.objects.filter(
        products__isnull=False
    ).annotate(
        product_count=django.db.models.Count('products')
    ).filter(
        product_count__gt=1
    ).distinct()

    print(f"📊 Found {bills_with_multiple_products.count()} vendor bills with multiple products")
    print("-" * 70)

    created_count = 0
    updated_count = 0
    skipped_count = 0
    error_count = 0

    for idx, bill in enumerate(bills_with_multiple_products, 1):
        print(f"🔄 Processing bill {idx}/{bills_with_multiple_products.count()}: {bill.bill_no}")

        try:
            # Get all products for this bill
            products = bill.products.all()
            product_count = products.count()

            if product_count <= 1:
                print(f"  ℹ️  Only {product_count} product(s), skipping consolidation")
                skipped_count += 1
                continue

            # Check if consolidated product already exists
            existing_consolidated = VendorZohoConsolidatedProduct.objects.filter(
                zohoBill=bill
            ).first()

            # Calculate consolidated data
            total_amount = sum(Decimal(str(p.amount or 0)) for p in products)

            # Create detailed breakdown
            item_details = []
            for product in products:
                rate = Decimal(str(product.rate or 0))
                qty = Decimal(str(product.quantity or 1))
                amount = Decimal(str(product.amount or 0))
                item_details.append(
                    f"• {product.item_name} (Qty: {qty}, Rate: ₹{rate}, Amount: ₹{amount})"
                )

            consolidated_details = f"Consolidated {product_count} items:\n" + "\n".join(item_details)
            consolidated_name = f"Consolidated Items - {bill.bill_no} ({product_count} items)"

            if existing_consolidated:
                # Update existing consolidated product
                existing_consolidated.consolidated_item_name = consolidated_name
                existing_consolidated.consolidated_item_details = consolidated_details
                existing_consolidated.total_quantity = Decimal('1')
                existing_consolidated.consolidated_rate = total_amount
                existing_consolidated.consolidated_amount = total_amount
                existing_consolidated.original_items_count = product_count
                existing_consolidated.consolidation_notes = f'Updated script run - {product_count} items consolidated'
                existing_consolidated.save()

                print(f"  ✅ Updated existing consolidated product (₹{total_amount})")
                updated_count += 1
            else:
                # Create new consolidated product
                VendorZohoConsolidatedProduct.objects.create(
                    zohoBill=bill,
                    organization=bill.organization,
                    consolidated_item_name=consolidated_name,
                    consolidated_item_details=consolidated_details,
                    total_quantity=Decimal('1'),
                    consolidated_rate=total_amount,
                    consolidated_amount=total_amount,
                    original_items_count=product_count,
                    consolidation_notes=f'Script created - {product_count} items consolidated',
                    itc_eligibility='eligible',
                    reverse_charge_tax_id=False
                )

                print(f"  ✅ Created new consolidated product (₹{total_amount})")
                created_count += 1

        except Exception as e:
            print(f"  ❌ Error processing bill {bill.bill_no}: {str(e)}")
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
        print("✅ Consolidated products are ready for Zoho vendor bills!")

        # Ask if user wants to set consolidate=True for bills with consolidated products
        response = input("\n🤔 Do you want to set consolidate=True for bills with consolidated products? (y/N): ")
        if response.lower() in ['y', 'yes']:
            update_consolidate_flags(created_count, updated_count)
    else:
        print("ℹ️  No consolidated products were created or updated")

def update_consolidate_flags(created_count, updated_count):
    """Update consolidate flags for bills with consolidated products"""
    print("\n🔄 Updating consolidate flags...")

    bills_updated = VendorZohoBill.objects.filter(
        consolidated_product__isnull=False,
        consolidate=False
    ).update(consolidate=True)

    print(f"✅ Updated {bills_updated} bills with consolidate=True")

if __name__ == "__main__":
    print("🎯 Zoho Vendor Bills - Consolidated Product Creation")
    print("This script will create consolidated products for bills with multiple line items.")
    print()

    try:
        create_consolidated_vendor_products()
        print("\n🎉 Script completed successfully!")
    except KeyboardInterrupt:
        print("\n⏹️  Script interrupted by user")
    except Exception as e:
        print(f"\n💥 Script failed with error: {str(e)}")
        import traceback
        traceback.print_exc()

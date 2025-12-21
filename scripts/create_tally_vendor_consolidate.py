#!/usr/bin/env python3
"""
Tally Vendor Bills - Consolidation Object Creation Script
Creates consolidated products for existing Tally vendor analyzed bills with multiple line items.

Usage: python create_tally_vendor_consolidate.py
"""

import os
import django
from decimal import Decimal

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.local')
django.setup()

import django.db.models
from apps.module.tally.models import (
    TallyVendorAnalyzedBill, TallyVendorAnalyzedProduct, TallyVendorConsolidatedProduct
)

def create_consolidated_vendor_products():
    """Create consolidated products for Tally vendor bills with multiple line items"""
    print("🚀 Starting consolidated product creation for existing Tally vendor bills...")
    print("=" * 70)

    # Find all analyzed vendor bills with multiple products (2 or more)
    bills_with_multiple_products = TallyVendorAnalyzedBill.objects.filter(
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
            existing_consolidated = TallyVendorConsolidatedProduct.objects.filter(
                vendor_bill_analyzed=analyzed_bill
            ).first()

            # Calculate consolidated data
            total_amount = sum(Decimal(str(p.amount or 0)) for p in products)

            # Create detailed breakdown
            item_details = []
            for product in products:
                price = Decimal(str(product.price or 0))
                qty = product.quantity or 1
                amount = Decimal(str(product.amount or 0))
                gst = product.product_gst or "18%"
                item_details.append(
                    f"• {product.item_name or product.item_details} (Qty: {qty}, Rate: ₹{price}, GST: {gst}, Amount: ₹{amount})"
                )

            consolidated_details = f"Consolidated {product_count} items:\n" + "\n".join(item_details)
            consolidated_name = f"Consolidated Items - {analyzed_bill.bill_no} ({product_count} items)"

            # Get most common GST rate from products
            gst_rates = [p.product_gst for p in products if p.product_gst]
            most_common_gst = max(set(gst_rates), key=gst_rates.count) if gst_rates else "18%"

            if existing_consolidated:
                # Update existing consolidated product
                existing_consolidated.item_name = consolidated_name
                existing_consolidated.item_details = consolidated_details
                existing_consolidated.price = total_amount
                existing_consolidated.quantity = 1
                existing_consolidated.amount = total_amount
                existing_consolidated.product_gst = most_common_gst
                existing_consolidated.igst = analyzed_bill.igst or 0
                existing_consolidated.cgst = analyzed_bill.cgst or 0
                existing_consolidated.sgst = analyzed_bill.sgst or 0
                existing_consolidated.original_items_count = product_count
                existing_consolidated.consolidation_notes = f'Updated script run - {product_count} items consolidated'
                existing_consolidated.save()

                print(f"  ✅ Updated existing consolidated product (₹{total_amount})")
                updated_count += 1
            else:
                # Create new consolidated product
                TallyVendorConsolidatedProduct.objects.create(
                    vendor_bill_analyzed=analyzed_bill,
                    organization=analyzed_bill.organization,
                    item_name=consolidated_name,
                    item_details=consolidated_details,
                    price=total_amount,
                    quantity=1,
                    amount=total_amount,
                    product_gst=most_common_gst,
                    igst=analyzed_bill.igst or 0,
                    cgst=analyzed_bill.cgst or 0,
                    sgst=analyzed_bill.sgst or 0,
                    original_items_count=product_count,
                    consolidation_notes=f'Script created - {product_count} items consolidated'
                )

                print(f"  ✅ Created new consolidated product (₹{total_amount}, GST: {most_common_gst})")
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
        print("✅ Consolidated products are ready for Tally vendor bills!")

        # Ask if user wants to set consolidate=True for bills with consolidated products
        response = input("\n🤔 Do you want to set consolidate=True for bills with consolidated products? (y/N): ")
        if response.lower() in ['y', 'yes']:
            update_consolidate_flags(created_count, updated_count)
    else:
        print("ℹ️  No consolidated products were created or updated")

def update_consolidate_flags(created_count, updated_count):
    """Update consolidate flags for analyzed bills with consolidated products"""
    print("\n🔄 Updating consolidate flags...")

    bills_updated = TallyVendorAnalyzedBill.objects.filter(
        consolidated_product__isnull=False,
        consolidate=False
    ).update(consolidate=True)

    print(f"✅ Updated {bills_updated} bills with consolidate=True")

if __name__ == "__main__":
    print("🎯 Tally Vendor Bills - Consolidated Product Creation")
    print("This script will create consolidated products for analyzed bills with multiple line items.")
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


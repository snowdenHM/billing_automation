#!/usr/bin/env python3
"""
Quick test to verify Zoho expense consolidate_prod fix is working
"""

import os
import django

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.local')
django.setup()

def test_expense_consolidate_prod_fix():
    """Test that expense detail view now returns consolidate_prod arrays"""
    from apps.module.zoho.models import ExpenseBill, ExpenseZohoBill
    from apps.module.zoho.serializers.expense_bills import ZohoExpenseBillDetailSerializer

    print("🔍 Testing Zoho Expense consolidate_prod fix...")

    # Find an expense bill with zoho data
    try:
        expense_bill = ExpenseBill.objects.filter(expensezohobill__isnull=False).first()
        if not expense_bill:
            print("❌ No expense bills with Zoho data found")
            return False

        print(f"✅ Testing bill: {expense_bill.billmunshiName}")

        # Get the zoho bill (like in the detail view)
        zoho_bill = ExpenseZohoBill.objects.select_related('selectBill').prefetch_related(
            'products__chart_of_accounts',
            'products__taxes',
            'consolidated_product'
        ).get(selectBill=expense_bill)

        # Attach to expense bill (like in detail view)
        expense_bill.zoho_bill = zoho_bill

        # Test the detail serializer (which should now include consolidate_prod)
        serializer = ZohoExpenseBillDetailSerializer(expense_bill, context={'request': None})
        data = serializer.data

        # Check if zoho_bill is in response
        if 'zoho_bill' in data:
            zoho_bill_data = data['zoho_bill']

            # Check if consolidate_prod is now in zoho_bill
            if 'consolidate_prod' in zoho_bill_data:
                consolidate_prod = zoho_bill_data['consolidate_prod']

                if isinstance(consolidate_prod, list):
                    products_count = len(zoho_bill_data.get('products', []))
                    consolidate_count = len(consolidate_prod)

                    print(f"✅ SUCCESS! consolidate_prod array found:")
                    print(f"   - Individual products: {products_count}")
                    print(f"   - consolidate_prod items: {consolidate_count}")

                    if consolidate_count > 0:
                        first_item = consolidate_prod[0]
                        print(f"   - First consolidated item amount: ₹{first_item.get('amount', 0)}")

                    print("🎉 FIX CONFIRMED: Zoho expense consolidate_prod arrays are now working!")
                    return True
                else:
                    print(f"❌ consolidate_prod is not array: {type(consolidate_prod)}")
                    return False
            else:
                print("❌ consolidate_prod key still missing from zoho_bill")
                return False
        else:
            print("❌ zoho_bill key missing from response")
            return False

    except Exception as e:
        print(f"❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = test_expense_consolidate_prod_fix()
    if success:
        print("\n✅ ZOHO EXPENSE CONSOLIDATE_PROD ARRAYS ARE NOW FIXED!")
    else:
        print("\n❌ Fix verification failed")

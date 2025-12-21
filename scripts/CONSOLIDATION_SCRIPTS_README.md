# 📊 BillMunshi Consolidation Scripts

This directory contains scripts to create consolidated products for existing bills across all BillMunshi services.

## 🎯 Purpose

These scripts are designed to transform older bills by creating consolidated objects, ensuring all existing bills have consolidated products available for the new consolidation feature.

## 📁 Available Scripts

### 1. **Zoho Services**
- `create_zoho_vendor_consolidate.py` - Zoho Vendor Bills
- `create_zoho_expense_consolidate.py` - Zoho Expense Bills  
- `create_zoho_journal_consolidate.py` - Zoho Journal Bills

### 2. **Tally Services**
- `create_tally_vendor_consolidate.py` - Tally Vendor Bills
- `create_tally_expense_consolidate.py` - Tally Expense Bills

### 3. **Master Script**
- `create_all_consolidate_products.py` - Runs all scripts together

## 🚀 Usage

### Run Individual Scripts
```bash
# Zoho Services
python create_zoho_vendor_consolidate.py
python create_zoho_expense_consolidate.py
python create_zoho_journal_consolidate.py

# Tally Services
python create_tally_vendor_consolidate.py
python create_tally_expense_consolidate.py

# All Services at Once
python create_all_consolidate_products.py
```

### Interactive Options
Each script will ask:
```
🤔 Do you want to set consolidate=True for bills with consolidated products? (y/N):
```

- **N (Default)**: Creates consolidated products but keeps `consolidate=False` (recommended)
- **Y**: Sets `consolidate=True` for bills with consolidated products

## 🔍 What Each Script Does

### 1. **Identifies Target Bills**
- Finds bills with 2 or more line items/products
- Skips bills with only 1 item (no consolidation needed)

### 2. **Creates/Updates Consolidated Products**
```python
# Example: Zoho Vendor
VendorZohoConsolidatedProduct.objects.create(
    zohoBill=bill,
    consolidated_item_name="Consolidated Items - INV001 (3 items)",
    consolidated_item_details="• Product A (₹500)\n• Product B (₹300)\n• Product C (₹200)",
    consolidated_amount=1000.00,
    original_items_count=3
)
```

### 3. **Provides Detailed Reporting**
```
📊 Found 25 vendor bills with multiple products
🔄 Processing bill 1/25: INV001
  ✅ Created new consolidated product (₹1500.00)
📈 SUMMARY:
   ✅ Consolidated products created: 20
   🔄 Consolidated products updated: 3
   ⏭️  Bills skipped: 2
```

## 📋 Output Format

### Zoho Bills
```json
{
  "consolidate": false,
  "products": [
    {"item_name": "Product A", "amount": 500},
    {"item_name": "Product B", "amount": 300}
  ],
  "consolidate_prod": [
    {
      "consolidated_item_name": "Consolidated Items (2 items)",
      "consolidated_amount": 800,
      "original_items_count": 2
    }
  ]
}
```

### Tally Bills
```json
{
  "consolidate": false,
  "products": [
    {"item_name": "Item A", "amount": 1000, "product_gst": "18%"},
    {"item_name": "Item B", "amount": 500, "product_gst": "12%"}
  ],
  "consolidate_prod": [
    {
      "item_name": "Consolidated Items (2 items)",
      "amount": 1500,
      "product_gst": "18%",
      "original_items_count": 2
    }
  ]
}
```

## ⚙️ Technical Details

### Database Models Used
```python
# Zoho
- VendorZohoConsolidatedProduct
- ExpenseZohoConsolidatedProduct  
- JournalZohoConsolidatedProduct

# Tally
- TallyVendorConsolidatedProduct
- TallyExpenseConsolidatedProduct
```

### Key Features
- **Safe Updates**: Checks for existing consolidated products
- **Error Handling**: Continues processing if individual bills fail
- **Detailed Logging**: Shows progress and results for each bill
- **Rollback Safety**: Uses Django transactions for data integrity

## 🛡️ Safety Features

### 1. **Non-Destructive**
- Creates new consolidated products without modifying existing data
- Original line items remain unchanged
- Can be run multiple times safely

### 2. **Error Recovery**
```python
try:
    # Process bill
    create_consolidated_product(bill)
except Exception as e:
    logger.error(f"Error processing {bill.id}: {e}")
    # Continue with next bill
```

### 3. **Validation**
- Skips bills with insufficient data
- Validates amounts and calculations
- Handles missing foreign keys gracefully

## 📊 Expected Results

### Before Running Scripts
```json
{
  "products": [{"id": 1, "amount": 500}, {"id": 2, "amount": 300}],
  "consolidate_prod": []  // Empty - no consolidated products
}
```

### After Running Scripts
```json
{
  "products": [{"id": 1, "amount": 500}, {"id": 2, "amount": 300}],
  "consolidate_prod": [   // Now has consolidated product
    {
      "id": "uuid",
      "consolidated_amount": 800,
      "original_items_count": 2
    }
  ]
}
```

## 🚦 Prerequisites

1. **Django Environment**: Scripts must be run in Django context
2. **Database Access**: Full read/write access to BillMunshi database
3. **Python Environment**: All required packages installed

## 🔧 Troubleshooting

### Common Issues
```bash
# ImportError: No module named 'apps'
# Solution: Run from project root directory
cd /path/to/billmunshi
python create_zoho_vendor_consolidate.py

# Django settings error
# Solution: Check DJANGO_SETTINGS_MODULE
export DJANGO_SETTINGS_MODULE=config.settings.local
```

### Verification
```python
# Check if consolidated products were created
from apps.module.zoho.models import VendorZohoConsolidatedProduct
count = VendorZohoConsolidatedProduct.objects.count()
print(f"Total consolidated products: {count}")
```

## 🎉 Post-Execution

After running the scripts:
1. ✅ All existing multi-item bills will have consolidated products
2. ✅ New bills will auto-create consolidated products during analysis
3. ✅ Frontend can display both individual and consolidated views
4. ✅ Verification APIs will handle consolidate_prod arrays
5. ✅ Sync logic will use consolidate flag to choose data format

## 📞 Support

If you encounter issues:
1. Check the error logs in script output
2. Verify database connections
3. Ensure proper Django settings
4. Run individual scripts to isolate problems

---
*These scripts complete the consolidation feature implementation by ensuring all existing bills have the necessary consolidated products for the new functionality.*

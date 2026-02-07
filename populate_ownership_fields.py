# Temporary script to populate ownership fields for existing bills
import os
import django

# Set up Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.development')
django.setup()

from apps.module.tally.models import TallyVendorBill, TallyExpenseBill
from apps.organizations.models import Organization

def validate_bill_ownership(json_data, organization):
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
        print(f"Error validating bill ownership: {str(e)}")
        return False, f"Validation error: {str(e)}"

def populate_vendor_bills():
    """Populate ownership fields for existing vendor bills"""
    print("Processing TallyVendorBill records...")
    
    bills_to_update = TallyVendorBill.objects.filter(
        analysed_data__isnull=False,
        bill_belong_your_org__isnull=True  # Only update bills that don't have ownership info
    ).exclude(analysed_data={})
    
    print(f"Found {bills_to_update.count()} vendor bills to update")
    
    updated_count = 0
    for bill in bills_to_update:
        if bill.analysed_data and bill.organization:
            bill_belongs_to_org, ownership_description = validate_bill_ownership(
                bill.analysed_data, bill.organization
            )
            
            bill.bill_belong_your_org = bill_belongs_to_org
            bill.description = ownership_description
            bill.save(update_fields=['bill_belong_your_org', 'description'])
            
            print(f"Updated bill {bill.bill_munshi_name}: {bill_belongs_to_org} - {ownership_description}")
            updated_count += 1
    
    print(f"Updated {updated_count} vendor bills")

def populate_expense_bills():
    """Populate ownership fields for existing expense bills"""
    print("\nProcessing TallyExpenseBill records...")
    
    bills_to_update = TallyExpenseBill.objects.filter(
        analysed_data__isnull=False,
        bill_belong_your_org__isnull=True  # Only update bills that don't have ownership info
    ).exclude(analysed_data={})
    
    print(f"Found {bills_to_update.count()} expense bills to update")
    
    updated_count = 0
    for bill in bills_to_update:
        if bill.analysed_data and bill.organization:
            bill_belongs_to_org, ownership_description = validate_bill_ownership(
                bill.analysed_data, bill.organization
            )
            
            bill.bill_belong_your_org = bill_belongs_to_org
            bill.description = ownership_description
            bill.save(update_fields=['bill_belong_your_org', 'description'])
            
            print(f"Updated bill {bill.bill_munshi_name}: {bill_belongs_to_org} - {ownership_description}")
            updated_count += 1
    
    print(f"Updated {updated_count} expense bills")

if __name__ == "__main__":
    print("Starting ownership fields population...")
    populate_vendor_bills()
    populate_expense_bills()
    print("\nOwnership fields population completed!")
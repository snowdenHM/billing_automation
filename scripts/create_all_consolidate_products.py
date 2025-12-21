#!/usr/bin/env python3
"""
Master Consolidation Script - Run All Services
Creates consolidated products for all existing bills across all services.

Usage: python create_all_consolidate_products.py
"""

import os
import sys
import subprocess
import time

def run_script(script_name, service_name):
    """Run a consolidation script and capture output"""
    print(f"🔄 Running {service_name} consolidation...")
    print("=" * 60)

    try:
        # Run the script
        result = subprocess.run([
            sys.executable, script_name
        ], capture_output=True, text=True, input='N\n')  # Default to N for consolidate flag

        # Print output
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print("STDERR:", result.stderr)

        if result.returncode == 0:
            print(f"✅ {service_name} completed successfully!")
        else:
            print(f"❌ {service_name} failed with return code {result.returncode}")
            return False

    except FileNotFoundError:
        print(f"❌ Script {script_name} not found!")
        return False
    except Exception as e:
        print(f"❌ Error running {service_name}: {str(e)}")
        return False

    print("\n" + "=" * 60 + "\n")
    time.sleep(1)  # Small delay between scripts
    return True

def main():
    """Run all consolidation scripts"""
    print("🚀 BillMunshi - Master Consolidation Script")
    print("This will create consolidated products for all services")
    print("=" * 60)

    scripts = [
        ("create_zoho_vendor_consolidate.py", "Zoho Vendor Bills"),
        ("create_zoho_expense_consolidate.py", "Zoho Expense Bills"),
        ("create_zoho_journal_consolidate.py", "Zoho Journal Bills"),
        ("create_tally_vendor_consolidate.py", "Tally Vendor Bills"),
        ("create_tally_expense_consolidate.py", "Tally Expense Bills"),
    ]

    results = []

    for script_file, service_name in scripts:
        print(f"\n🎯 Starting {service_name}...")
        success = run_script(script_file, service_name)
        results.append((service_name, success))

    # Final summary
    print("🏁 FINAL SUMMARY")
    print("=" * 60)

    successful = 0
    failed = 0

    for service_name, success in results:
        if success:
            print(f"✅ {service_name}: SUCCESS")
            successful += 1
        else:
            print(f"❌ {service_name}: FAILED")
            failed += 1

    print("-" * 60)
    print(f"📊 Total: {len(results)} services")
    print(f"✅ Successful: {successful}")
    print(f"❌ Failed: {failed}")

    if failed == 0:
        print("\n🎉 All consolidation scripts completed successfully!")
        print("✅ Your BillMunshi platform now has consolidated products for all existing bills!")

        print("\n💡 Next steps:")
        print("1. Test the consolidation feature in the frontend")
        print("2. Verify API responses include consolidate_prod arrays")
        print("3. Test verification with consolidate_prod updates")
        print("4. Run individual scripts with 'y' to set consolidate=True if needed")
    else:
        print(f"\n⚠️  {failed} script(s) failed. Please check the errors above.")
        print("You can run individual scripts to debug issues.")

    print("\n📝 Available individual scripts:")
    for script_file, service_name in scripts:
        print(f"   - python {script_file}  # {service_name}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n⏹️  Master script interrupted by user")
    except Exception as e:
        print(f"\n💥 Master script failed: {str(e)}")
        import traceback
        traceback.print_exc()

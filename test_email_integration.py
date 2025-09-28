#!/usr/bin/env python
"""
Simple test script to verify email integration is working properly.
Run this with: python test_email_integration.py
"""
import os
import sys
import django

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.local')
sys.path.insert(0, '/Users/snowden/private/clients/billmunshi')

django.setup()

from django.core.mail import send_mail
from django.conf import settings
from apps.organizations.serializers import OrgMembershipSerializer

def test_email_settings():
    """Test basic email configuration"""
    print("=== Email Configuration Test ===")
    print(f"EMAIL_BACKEND: {settings.EMAIL_BACKEND}")
    print(f"DEFAULT_FROM_EMAIL: {settings.DEFAULT_FROM_EMAIL}")

    if hasattr(settings, 'EMAIL_HOST'):
        print(f"EMAIL_HOST: {settings.EMAIL_HOST}")
        print(f"EMAIL_PORT: {settings.EMAIL_PORT}")
        print(f"EMAIL_USE_TLS: {settings.EMAIL_USE_TLS}")

    return True

def test_send_simple_email():
    """Test sending a simple email"""
    print("\n=== Simple Email Test ===")
    try:
        send_mail(
            'Test Email from BillMunshi',
            'This is a test email to verify email integration is working.',
            settings.DEFAULT_FROM_EMAIL,
            ['test@example.com'],
            fail_silently=False,
        )
        print("✅ Email sent successfully!")
        return True
    except Exception as e:
        print(f"❌ Email failed: {str(e)}")
        return False

def test_password_generation():
    """Test password generation function"""
    print("\n=== Password Generation Test ===")
    serializer = OrgMembershipSerializer()

    # Generate 5 test passwords
    passwords = [serializer._generate_random_password() for _ in range(5)]

    for i, password in enumerate(passwords, 1):
        print(f"Password {i}: {password} (length: {len(password)})")

    # Check all passwords are unique
    if len(set(passwords)) == len(passwords):
        print("✅ All passwords are unique!")
        return True
    else:
        print("❌ Some passwords are duplicated!")
        return False

def main():
    """Run all email integration tests"""
    print("🚀 Testing BillMunshi Email Integration\n")

    tests = [
        test_email_settings,
        test_send_simple_email,
        test_password_generation
    ]

    results = []
    for test in tests:
        try:
            results.append(test())
        except Exception as e:
            print(f"❌ Test {test.__name__} failed with error: {str(e)}")
            results.append(False)

    print(f"\n📊 Test Results: {sum(results)}/{len(results)} passed")

    if all(results):
        print("🎉 All email integration tests passed!")
    else:
        print("⚠️  Some tests failed. Check configuration.")

if __name__ == '__main__':
    main()

from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password

User = get_user_model()

class Command(BaseCommand):
    help = 'Fix user passwords by setting them to a known value with proper Django hashing'

    def handle(self, *args, **options):
        # Get all users
        users = User.objects.all()
        default_password = 'Welcome@123'  # You can change this default password

        for user in users:
            # Update password with proper Django hashing
            user.password = make_password(default_password)
            user.save()
            self.stdout.write(self.style.SUCCESS(f'Successfully updated password for user {user.email}'))

        self.stdout.write(self.style.SUCCESS('All passwords have been updated to: Welcome@123'))

from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
import os

User = get_user_model()

class Command(BaseCommand):
    help = 'Fix user passwords by setting them to a known value with proper Django hashing'

    def add_arguments(self, parser):
        parser.add_argument('--password', type=str, help='Password to set (or set DEFAULT_RESET_PASSWORD env var)')

    def handle(self, *args, **options):
        default_password = options.get('password') or os.environ.get('DEFAULT_RESET_PASSWORD', 'ChangeMeNow!1')
        users = User.objects.all()

        for user in users:
            user.password = make_password(default_password)
            user.save()
            self.stdout.write(self.style.SUCCESS(f'Successfully updated password for user {user.email}'))

        self.stdout.write(self.style.SUCCESS(f'All {users.count()} passwords have been updated.'))

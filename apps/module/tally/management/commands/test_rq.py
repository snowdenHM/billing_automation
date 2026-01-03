from django.core.management.base import BaseCommand
import django_rq
from django_rq import get_connection
from apps.module.tally.tasks import process_vendor_bill_analysis, process_expense_bill_analysis


class Command(BaseCommand):
    help = 'Test Django-RQ setup and connection'

    def handle(self, *args, **options):
        self.stdout.write("Testing Django-RQ setup...")
        
        try:
            # Test Redis connection
            connection = get_connection('default')
            connection.ping()
            self.stdout.write(
                self.style.SUCCESS(f'✅ Redis connection successful: {connection}')
            )
            
            # Test queue access
            queue = django_rq.get_queue('default')
            self.stdout.write(
                self.style.SUCCESS(f'✅ Default queue accessible: {queue}')
            )
            
            # Test high priority queue
            high_queue = django_rq.get_queue('high')
            self.stdout.write(
                self.style.SUCCESS(f'✅ High priority queue accessible: {high_queue}')
            )
            
            # Test low priority queue
            low_queue = django_rq.get_queue('low')
            self.stdout.write(
                self.style.SUCCESS(f'✅ Low priority queue accessible: {low_queue}')
            )
            
            # Test job enqueue (fake job for testing)
            job = queue.enqueue(
                lambda: "Test job completed successfully",
                timeout=60
            )
            self.stdout.write(
                self.style.SUCCESS(f'✅ Test job enqueued successfully: {job.id}')
            )
            
            # Show queue stats
            self.stdout.write(f"\n📊 Queue Statistics:")
            for queue_name in ['default', 'high', 'low']:
                q = django_rq.get_queue(queue_name)
                self.stdout.write(f"  {queue_name}: {len(q)} jobs pending")
            
            self.stdout.write(f"\n🚀 Django-RQ is properly configured!")
            self.stdout.write(f"📍 Next steps:")
            self.stdout.write(f"   1. Start Redis: redis-server")
            self.stdout.write(f"   2. Start workers: python manage.py rqworker default")
            self.stdout.write(f"   3. Monitor jobs: http://localhost:8000/admin/rq/")
            
        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f'❌ Django-RQ setup error: {str(e)}')
            )
            self.stdout.write(f"\n🔧 Troubleshooting:")
            self.stdout.write(f"   1. Make sure Redis is running: redis-server")
            self.stdout.write(f"   2. Check Redis connection: redis-cli ping")
            self.stdout.write(f"   3. Verify .env variables: REDIS_HOST, REDIS_PORT, etc.")
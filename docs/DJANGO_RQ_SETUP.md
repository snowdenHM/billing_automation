# Django-RQ Configuration Guide

## 1. Environment Variables (.env file)

Add these to your .env file:

```bash
# Redis Configuration for Django-RQ
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_DB=0
REDIS_PASSWORD=
```

## 2. Start Redis Server

```bash
# On macOS with Homebrew
brew services start redis

# Or manually
redis-server

# Test Redis connection
redis-cli ping
```

## 3. Run RQ Workers

Open separate terminals for each queue:

```bash
# Default queue worker (most important)
python manage.py rqworker default

# High priority queue worker (optional)
python manage.py rqworker high

# Low priority queue worker (optional)
python manage.py rqworker low
```

## 4. Monitor Jobs

- Admin interface: http://localhost:8000/admin/rq/
- Or use RQ Dashboard: `pip install rq-dashboard && rq-dashboard`

## 5. Test the Setup

```python
# In Django shell
python manage.py shell

from apps.module.tally.tasks import enqueue_vendor_bill_processing
import django_rq

# Test queue connection
queue = django_rq.get_queue('default')
print(queue.connection)  # Should show Redis connection

# Test job enqueue (replace with real bill ID)
# job = enqueue_vendor_bill_processing('some-bill-id-here')
# print(job.id)
```

## 6. Production Setup

For production, consider:

- Redis persistence configuration
- Supervisor or systemd for worker processes
- Multiple worker processes
- Monitoring with Sentry or similar

## 7. Current Configuration

- Default queue: 6-minute timeout
- High/Low queues: 8.5-minute timeout
- Redis DB 0 (default)
- Configurable via environment variables

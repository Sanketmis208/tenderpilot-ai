"""Celery application: separate queues for cheap, expensive (parsing/OCR) and AI work."""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab
from kombu import Queue

from app.core.config import get_settings

s = get_settings()

celery_app = Celery("tenderpilot", broker=s.redis_url, backend=s.redis_url, include=["app.workers.tasks"])
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,  # redeliver if a worker dies mid-task (tasks are idempotent)
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_time_limit=60 * 30,
    task_soft_time_limit=60 * 25,
    task_queues=(Queue("default"), Queue("documents"), Queue("ai"), Queue("notifications")),
    task_default_queue="default",
    task_always_eager=s.celery_eager,
    task_eager_propagates=False,
    broker_transport_options={"visibility_timeout": 60 * 60},
    beat_schedule={
        "deadline-reminders": {"task": "tp.scheduled.deadline_reminders", "schedule": crontab(minute=0)},
        "document-expiry-warnings": {"task": "tp.scheduled.document_expiry", "schedule": crontab(hour=3, minute=30)},
        "retry-email-deliveries": {"task": "tp.scheduled.retry_emails", "schedule": crontab(minute="*/15")},
        "purge-deleted-organizations": {"task": "tp.scheduled.purge_orgs", "schedule": crontab(hour=2, minute=0)},
        "requeue-stuck-jobs": {"task": "tp.scheduled.requeue_stuck", "schedule": crontab(minute="*/10")},
        # Each feed has its own interval (15 min to 24 h); this only checks which ones are due.
        "poll-tender-feeds": {"task": "tp.scheduled.poll_feeds", "schedule": crontab(minute="*/15")},
    },
)

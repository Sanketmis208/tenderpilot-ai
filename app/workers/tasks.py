"""Celery tasks. Business logic lives in services; tasks only drive the durable job runner."""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select

from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.models import JobStatus, ProcessingJob
from app.db.session import session_scope
from app.workers.celery_app import celery_app

log = get_logger(__name__)


def _load_handlers() -> None:
    # Registers @handler functions (import side effect, kept explicit).
    import app.services.documents.pipeline  # noqa: F401
    import app.services.eligibility.service  # noqa: F401
    import app.services.tenders.pipeline  # noqa: F401


@celery_app.task(name="tp.run_job", bind=True, max_retries=5)
def run_job(self, job_id: str) -> str:
    from app.services import jobs

    _load_handlers()
    status = jobs.execute(uuid.UUID(job_id))
    if status == JobStatus.RETRYING and not self.request.is_eager:
        # Exponential backoff with jitter: 30s, 60s, 120s ...
        raise self.retry(countdown=min(30 * 2**self.request.retries, 900))
    if status == JobStatus.RETRYING and self.request.is_eager:
        status = jobs.execute(uuid.UUID(job_id))
    return status


@celery_app.task(name="tp.scheduled.deadline_reminders")
def deadline_reminders() -> int:
    from app.services.notifications.service import send_deadline_reminders

    with session_scope() as db:
        return send_deadline_reminders(db)


@celery_app.task(name="tp.scheduled.document_expiry")
def document_expiry() -> int:
    from app.services.notifications.service import send_expiry_warnings

    with session_scope() as db:
        return send_expiry_warnings(db)


@celery_app.task(name="tp.scheduled.retry_emails")
def retry_emails() -> int:
    from app.services.notifications.service import retry_failed_emails

    with session_scope() as db:
        return retry_failed_emails(db)


@celery_app.task(name="tp.scheduled.purge_orgs")
def purge_orgs() -> int:
    from app.services.organizations.retention import purge_due_organizations

    with session_scope() as db:
        return purge_due_organizations(db)


@celery_app.task(name="tp.scheduled.requeue_stuck")
def requeue_stuck() -> int:
    """Re-dispatch jobs left queued (broker outage) or running past the hard time limit (dead worker)."""
    cutoff_q = utcnow() - timedelta(minutes=10)
    cutoff_r = utcnow() - timedelta(minutes=40)
    n = 0
    with session_scope() as db:
        rows = db.scalars(
            select(ProcessingJob).where(
                ((ProcessingJob.status == JobStatus.QUEUED) & (ProcessingJob.created_at < cutoff_q))
                | ((ProcessingJob.status == JobStatus.RUNNING) & (ProcessingJob.started_at < cutoff_r))
            ).limit(100)
        ).all()
        for job in rows:
            if job.status == JobStatus.RUNNING and job.attempts >= job.max_attempts:
                job.status, job.error, job.finished_at = JobStatus.FAILED, "Worker lost; attempts exhausted", utcnow()
                continue
            job.status = JobStatus.QUEUED
            run_job.apply_async(args=[str(job.id)], queue=job.queue)
            n += 1
    return n

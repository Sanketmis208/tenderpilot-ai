"""Durable job records + dispatch. The DB row is the source of truth for job state, not Celery."""

from __future__ import annotations

import traceback
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.context import TenantContext
from app.core.config import get_settings
from app.core.errors import NotFound
from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.models import JobStatus, ProcessingJob
from app.db.session import session_scope
from app.db.tenancy import system_scope, tenant_scope

log = get_logger(__name__)

QUEUE_FOR = {
    "document.process": "documents",
    "tender.extract": "documents",
    "tender.amendment": "documents",
    "eligibility.assess": "ai",
    "bid.generate": "ai",
    "boq.analyse": "documents",
    "notification.email": "notifications",
}


def enqueue(
    db: Session,
    *,
    kind: str,
    organization_id: uuid.UUID,
    subject_type: str,
    subject_id: uuid.UUID,
    idempotency_key: str,
    created_by_id: uuid.UUID | None = None,
    payload: dict[str, Any] | None = None,
    max_attempts: int = 3,
) -> ProcessingJob:
    """Create (or return the existing) job for an idempotency key, then dispatch after commit."""
    existing = db.scalar(select(ProcessingJob).where(ProcessingJob.idempotency_key == idempotency_key))
    if existing and existing.status not in (JobStatus.FAILED, JobStatus.CANCELLED):
        return existing
    if existing:
        existing.status, existing.attempts, existing.error, existing.progress = JobStatus.QUEUED, 0, None, 0
        job = existing
    else:
        job = ProcessingJob(
            organization_id=organization_id,
            kind=kind,
            subject_type=subject_type,
            subject_id=subject_id,
            idempotency_key=idempotency_key,
            created_by_id=created_by_id,
            queue=QUEUE_FOR.get(kind, "default"),
            max_attempts=max_attempts,
            result={"payload": payload or {}},
        )
        db.add(job)
    db.flush()
    _dispatch_after_commit(db, job.id, job.queue)
    return job


def _dispatch_after_commit(db: Session, job_id: uuid.UUID, queue: str) -> None:
    from sqlalchemy import event

    def _send(_session: Session) -> None:
        from app.workers.tasks import run_job

        s = get_settings()
        if s.celery_eager:
            run_job.apply(args=[str(job_id)])
        else:
            try:
                run_job.apply_async(args=[str(job_id)], queue=queue)
            except Exception as exc:  # broker down: job stays queued; the requeue sweeper retries
                log.error("job.dispatch_failed", job_id=str(job_id), error=str(exc))

    event.listen(db, "after_commit", _send, once=True)


HANDLERS: dict[str, Callable[[Session, ProcessingJob, Callable[[int, str], None]], dict]] = {}


def handler(kind: str):
    def deco(fn):
        HANDLERS[kind] = fn
        return fn

    return deco


class JobCancelled(Exception):
    pass


class RetryableJobError(Exception):
    """Raise for transient failures (provider timeouts, network) — the job is retried with backoff."""


@contextmanager
def _job_session(job_id: uuid.UUID):
    with session_scope() as db:
        job = db.get(ProcessingJob, job_id, with_for_update=True)
        yield db, job


def execute(job_id: uuid.UUID) -> str:
    """Run a job once. Returns the resulting status. Safe to call repeatedly (idempotent).

    The job's organization is looked up in system scope; everything else (handler included) runs scoped to that
    organization, so a worker thread can never touch another tenant's rows while processing this job."""
    with system_scope(), session_scope() as db:
        org_id = db.scalar(select(ProcessingJob.organization_id).where(ProcessingJob.id == job_id))
    if org_id is None:
        return "missing"
    with tenant_scope(org_id):
        return _execute(job_id)


def _execute(job_id: uuid.UUID) -> str:
    with _job_session(job_id) as (db, job):
        if job is None:
            return "missing"
        if job.status in (JobStatus.SUCCEEDED, JobStatus.CANCELLED):
            return job.status
        if job.cancel_requested:
            job.status, job.finished_at = JobStatus.CANCELLED, utcnow()
            return job.status
        job.status, job.attempts, job.started_at = JobStatus.RUNNING, job.attempts + 1, utcnow()
        job.stage, job.error = "starting", None
    fn = HANDLERS.get(job.kind)
    try:
        if fn is None:
            raise RuntimeError(f"No handler for job kind {job.kind}")
        with session_scope() as db:
            j = db.get(ProcessingJob, job_id)

            def progress(pct: int, stage: str) -> None:
                # Progress is written in its own short transaction so the UI sees it live.
                with session_scope() as pdb:
                    pj = pdb.get(ProcessingJob, job_id)
                    if pj:
                        if pj.cancel_requested:
                            raise JobCancelled()
                        pj.progress, pj.stage = pct, stage

            result = fn(db, j, progress)  # type: ignore[arg-type]
        with _job_session(job_id) as (db, j):
            j.status, j.progress, j.stage, j.finished_at = JobStatus.SUCCEEDED, 100, "done", utcnow()
            j.result = {**(j.result or {}), "output": result or {}}
            return j.status
    except JobCancelled:
        with _job_session(job_id) as (db, j):
            j.status, j.finished_at, j.stage = JobStatus.CANCELLED, utcnow(), "cancelled"
            return j.status
    except Exception as exc:
        log.error("job.failed", job_id=str(job_id), kind=job.kind, error=str(exc))
        with _job_session(job_id) as (db, j):
            retryable = isinstance(exc, RetryableJobError) and j.attempts < j.max_attempts
            j.error = f"{type(exc).__name__}: {exc}"[:4000]
            j.result = {**(j.result or {}), "traceback": traceback.format_exc()[-4000:]}
            j.status = JobStatus.RETRYING if retryable else JobStatus.FAILED
            if not retryable:
                j.finished_at = utcnow()
                _on_final_failure(db, j)
            return j.status


def _on_final_failure(db: Session, job: ProcessingJob) -> None:
    """Dead-letter handling: mark the subject failed and notify the job creator."""
    from app.services.notifications.service import notify

    if job.created_by_id:
        notify(
            db,
            organization_id=job.organization_id,
            user_id=job.created_by_id,
            kind="job_failed",
            title=f"Processing failed: {job.kind.replace('.', ' ')}",
            body=(job.error or "")[:300],
            link=None,
            dedup_key=f"job_failed:{job.id}",
        )


def get_job(db: Session, ctx: TenantContext, job_id: uuid.UUID) -> ProcessingJob:
    job = db.scalar(
        select(ProcessingJob).where(ProcessingJob.id == job_id, ProcessingJob.organization_id == ctx.organization_id)
    )
    if not job:
        raise NotFound("Job not found.")
    return job


def cancel_job(db: Session, ctx: TenantContext, job_id: uuid.UUID) -> ProcessingJob:
    job = get_job(db, ctx, job_id)
    if job.status in (JobStatus.QUEUED, JobStatus.RETRYING):
        job.status, job.finished_at = JobStatus.CANCELLED, utcnow()
    elif job.status == JobStatus.RUNNING:
        job.cancel_requested = True  # honoured at the next progress checkpoint
    return job


def jobs_for_subject(db: Session, ctx: TenantContext, subject_id: uuid.UUID) -> list[ProcessingJob]:
    return list(
        db.scalars(
            select(ProcessingJob)
            .where(ProcessingJob.organization_id == ctx.organization_id, ProcessingJob.subject_id == subject_id)
            .order_by(ProcessingJob.created_at.desc())
        )
    )

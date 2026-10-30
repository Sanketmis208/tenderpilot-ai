"""Reusable UI components bound to real services (uploads, job progress, downloads)."""

from __future__ import annotations

import uuid
from collections.abc import Callable

from nicegui import ui
from sqlalchemy import select

from app.core.config import get_settings
from app.core.errors import AppError
from app.db.models import ProcessingJob
from app.db.session import session_scope
from app.services.documents import service as documents
from app.ui.themes.theme import JOB_COLOR


def job_tracker(job_ids: list[uuid.UUID], on_done: Callable[[], None] | None = None) -> None:
    """Live progress for background jobs, read from the durable job table (no simulated progress)."""
    if not job_ids:
        return
    container = ui.column().classes("w-full gap-1")
    done_fired = {"v": False}

    def poll():
        with session_scope() as db:
            rows = db.scalars(select(ProcessingJob).where(ProcessingJob.id.in_(job_ids))).all()
            data = [(j.kind, j.status, j.progress, j.stage, j.error) for j in rows]
        container.clear()
        with container:
            for kind, status, progress, stage, error in data:
                with ui.row().classes("w-full items-center no-wrap gap-2"):
                    ui.badge(status, color=JOB_COLOR.get(status, "grey")).props("outline")
                    ui.label(kind.replace(".", " › ")).classes("text-caption").style("min-width:140px")
                    ui.linear_progress(value=progress / 100, show_value=False).props("rounded").classes("col")
                    ui.label(error[:120] if error else (stage or "")).classes("text-caption " + ("text-negative" if error else "tp-muted"))
        if data and all(s in ("succeeded", "failed", "cancelled") for _, s, *_ in data):
            timer.deactivate()
            if on_done and not done_fired["v"]:
                done_fired["v"] = True
                on_done()

    timer = ui.timer(1.0, poll)
    poll()


def upload_box(pc, *, scope: str = "company", tender=None, role: str = "main", document_id: uuid.UUID | None = None,
               on_done: Callable[[], None] | None = None, label: str = "Drop files or click to upload (PDF, DOCX, XLSX, CSV, TXT, ZIP)") -> None:
    tracker = ui.column().classes("w-full")

    async def handle(e):
        data = await e.file.read()
        name = e.file.name
        try:
            with session_scope() as db:
                if tender is not None:
                    from app.services.tenders import service as tenders

                    t = tenders.get_tender(db, pc.ctx, tender)
                    results = tenders.attach_document(db, pc.ctx, t, data, name, role)
                else:
                    results = documents.upload(db, pc.ctx, data, name, scope=scope, document_id=document_id)
                ids = [r.job.id for r in results if r.job]
                dedup = any(r.deduplicated for r in results)
                warnings = [w for r in results for w in (r.warnings or [])]
        except AppError as exc:
            ui.notify(f"{name}: {exc.message}", type="negative", multi_line=True)
            return
        if dedup:
            ui.notify(f"{name} is identical to the current version — no new version created.", type="info")
        for w in warnings[:5]:
            ui.notify(w, type="warning")
        ui.notify(f"{name} uploaded — processing started", type="positive")
        with tracker:
            job_tracker(ids, on_done)

    ui.upload(label=label, multiple=True, auto_upload=True, max_file_size=get_settings().max_upload_bytes, on_upload=handle,
              on_rejected=lambda: ui.notify("File rejected (too large?)", type="negative")).props("accept=.pdf,.docx,.xlsx,.csv,.txt,.zip flat bordered").classes("w-full")


def download_button(pc, version_id: uuid.UUID, label: str = "Download", icon: str = "download", props: str = "flat dense color=primary") -> None:
    def go():
        try:
            with session_scope() as db:
                url = documents.download_url(db, pc.ctx, version_id)
            ui.navigate.to(url, new_tab=True)
        except AppError as exc:
            ui.notify(exc.message, type="negative")

    ui.button(label, icon=icon, on_click=go).props(props)

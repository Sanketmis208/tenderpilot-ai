"""Bid workspaces, tasks, comments, drafts, approvals, exports and BOQ."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth.context import TenantContext
from app.auth.deps import ctx_dep
from app.db.session import get_db
from app.schemas import api as s
from app.services.bids import generation
from app.services.bids import service as bids
from app.services.boq import service as boq

router = APIRouter(tags=["bids & boq"])


@router.post("/tenders/{tender_id}/workspace", response_model=s.WorkspaceOut, status_code=201)
def create_workspace(tender_id: uuid.UUID, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return bids.create_workspace(db, ctx, tender_id)


@router.get("/workspaces")
def list_workspaces(ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return [{"workspace": s.WorkspaceOut.model_validate(w).model_dump(mode="json"), "tender_title": t.title, "deadline": t.submission_deadline, "tasks": c}
            for w, t, c in bids.list_workspaces(db, ctx)]


@router.get("/workspaces/{workspace_id}", response_model=s.WorkspaceOut)
def get_workspace(workspace_id: uuid.UUID, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return bids.get_workspace(db, ctx, workspace_id)


@router.get("/workspaces/{workspace_id}/tasks", response_model=list[s.TaskOut])
def tasks(workspace_id: uuid.UUID, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return bids.list_tasks(db, ctx, workspace_id)


@router.post("/workspaces/{workspace_id}/tasks", response_model=s.TaskOut, status_code=201)
def create_task(workspace_id: uuid.UUID, body: s.TaskIn, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return bids.create_task(db, ctx, workspace_id, **body.model_dump())


@router.patch("/tasks/{task_id}", response_model=s.TaskOut)
def update_task(task_id: uuid.UUID, body: s.TaskPatch, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return bids.update_task(db, ctx, task_id, body.changes, body.lock_version)


@router.post("/workspaces/{workspace_id}/comments", status_code=201)
def comment(workspace_id: uuid.UUID, body: s.CommentIn, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    c = bids.add_comment(db, ctx, workspace_id, body.body, task_id=body.task_id, draft_id=body.draft_id)
    db.flush()
    return {"id": str(c.id)}


@router.get("/workspaces/{workspace_id}/comments")
def comments(workspace_id: uuid.UUID, task_id: uuid.UUID | None = None, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return [{"id": str(c.id), "author_id": str(c.author_id), "body": c.body, "task_id": str(c.task_id) if c.task_id else None, "created_at": c.created_at}
            for c in bids.comments(db, ctx, workspace_id, task_id=task_id)]


@router.post("/workspaces/{workspace_id}/drafts/generate", response_model=list[s.DraftOut], status_code=201)
def generate(workspace_id: uuid.UUID, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return generation.generate_drafts(db, ctx, workspace_id)


@router.get("/workspaces/{workspace_id}/drafts", response_model=list[s.DraftOut])
def drafts(workspace_id: uuid.UUID, all_versions: bool = False, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return bids.drafts(db, ctx, workspace_id, current_only=not all_versions)


@router.put("/drafts/{draft_id}", response_model=s.DraftOut)
def edit_draft(draft_id: uuid.UUID, body: s.DraftEditIn, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return bids.edit_draft(db, ctx, draft_id, body.sections, body.lock_version)


@router.post("/workspaces/{workspace_id}/approval", response_model=s.WorkspaceOut)
def approval(workspace_id: uuid.UUID, body: s.ApprovalIn, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return bids.approval_action(db, ctx, workspace_id, body.action, body.note)


@router.get("/workspaces/{workspace_id}/approvals")
def approvals(workspace_id: uuid.UUID, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return [{"action": a.action, "from": a.from_status, "to": a.to_status, "draft_version": a.draft_version_no, "actor_id": str(a.actor_id) if a.actor_id else None,
             "note": a.note, "at": a.created_at} for a in bids.approvals(db, ctx, workspace_id)]


@router.post("/workspaces/{workspace_id}/export", response_model=list[s.ArtifactOut], status_code=201)
def export(workspace_id: uuid.UUID, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return generation.export_package(db, ctx, workspace_id)


@router.get("/workspaces/{workspace_id}/artifacts", response_model=list[s.ArtifactOut])
def artifacts(workspace_id: uuid.UUID, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return bids.artifacts(db, ctx, workspace_id)


@router.get("/workspaces/{workspace_id}/timeline")
def timeline(workspace_id: uuid.UUID, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return [{"action": e.action, "actor_id": str(e.actor_id) if e.actor_id else None, "data": e.data, "at": e.created_at} for e in bids.timeline(db, ctx, workspace_id)]


@router.post("/tenders/{tender_id}/boq", status_code=201)
def import_boq(tender_id: uuid.UUID, document_version_id: uuid.UUID | None = None, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    imp = boq.import_boq(db, ctx, tender_id, document_version_id)
    return {"id": str(imp.id), "summary": imp.summary, "issues": imp.issues, "columns": imp.column_map, "sheet": imp.sheet_name}


@router.get("/boq/{import_id}/items")
def boq_items(import_id: uuid.UUID, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    return [{"id": str(i.id), "row": i.row_no, "code": i.item_code, "description": i.description, "unit": i.unit,
             "quantity": str(i.quantity) if i.quantity is not None else None, "quoted_rate": str(i.quoted_rate) if i.quoted_rate is not None else None,
             "rate_cell": i.rate_cell, "locked": i.rate_cell_locked, "formula": i.amount_formula, "heading": i.is_heading, "subtotal": i.is_subtotal,
             "issues": i.issues} for i in boq.items(db, ctx, import_id)]


@router.post("/boq/{import_id}/scenarios", status_code=201)
def save_scenario(import_id: uuid.UUID, body: s.ScenarioIn, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    sc = boq.save_scenario(db, ctx, import_id, body.name, body.rates, body.markup_pct, body.gst_pct)
    return {"id": str(sc.id), "totals": sc.totals}


@router.post("/boq/scenarios/{scenario_id}/export", status_code=201)
def export_boq(scenario_id: uuid.UUID, ctx: TenantContext = Depends(ctx_dep), db: Session = Depends(get_db)):
    ver = boq.export_priced_copy(db, ctx, scenario_id)
    return {"document_id": str(ver.document_id), "version_id": str(ver.id)}

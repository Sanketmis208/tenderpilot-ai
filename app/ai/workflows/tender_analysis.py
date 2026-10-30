"""Tender analysis workflow (LangGraph).

  tender_intelligence → company_intelligence → eligibility_analyst → risk_review
        → human_review (interrupt when mandatory items need a person) → remediation → finalize

Design rules:
- Each node commits its DB work before returning, so a checkpoint never claims work that was
  rolled back; every node is idempotent so a resumed/retried node cannot duplicate rows.
- The eligibility node calls the deterministic rule engine; LLM output (when configured) is
  limited to narrative summaries and never changes a decision.
- Steps are bounded (recursion_limit) and cancellation is checked at every node.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai.llm.client import LLMRefused, RunBudget, get_llm
from app.ai.workflows.checkpointer import checkpointer
from app.core.config import get_settings
from app.core.errors import QuotaExceeded, ServiceUnavailable
from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.models import (
    AgentRun,
    AssessmentStatus,
    BidTask,
    BidWorkspace,
    DecisionStatus,
    EligibilityAssessment,
    EligibilityDecision,
    EvidenceConflict,
    Tender,
    TenderRequirement,
)
from app.extraction.normalization.values import IST, format_inr
from app.services.eligibility import service as elig
from app.services.evidence.gather import load_company_evidence

log = get_logger(__name__)


class AnalysisState(TypedDict, total=False):
    assessment_id: str
    organization_id: str
    tender_id: str
    run_id: str
    tender_brief: dict[str, Any]
    company_brief: dict[str, Any]
    eligibility: dict[str, Any]
    risks: list[dict[str, Any]]
    review_items: list[dict[str, Any]]
    review_outcome: dict[str, Any]
    remediation: dict[str, Any]
    narrative: dict[str, Any]


class RunCancelled(Exception):
    pass


def _db_from(config) -> Session:
    return config["configurable"]["db"]


def _track(db: Session, state: AnalysisState, node: str, summary: dict[str, Any]) -> None:
    run = db.get(AgentRun, uuid.UUID(state["run_id"]))
    if run is None:
        return
    if run.cancel_requested:
        run.status, run.finished_at = "cancelled", utcnow()
        db.commit()
        raise RunCancelled()
    run.steps += 1
    if run.steps > get_settings().agent_max_steps:
        raise QuotaExceeded("Agent step limit reached.")
    run.history = [*run.history, {"node": node, "at": utcnow().isoformat(), **summary}]
    db.commit()


def tender_intelligence(state: AnalysisState, config) -> dict:
    db = _db_from(config)
    t = db.get(Tender, uuid.UUID(state["tender_id"]))
    reqs = db.scalars(select(TenderRequirement).where(TenderRequirement.tender_id == t.id, TenderRequirement.is_current.is_(True))).all()
    brief = {
        "title": t.title,
        "reference": t.reference_number,
        "submission_deadline_ist": t.submission_deadline.astimezone(IST).strftime("%d %b %Y %H:%M") if t.submission_deadline else None,
        "estimated_value": format_inr(t.estimated_value) if t.estimated_value else None,
        "requirements": len(reqs),
        "mandatory": sum(1 for r in reqs if r.mandatory in ("mandatory", "conditional")),
        "by_type": {k: sum(1 for r in reqs if r.req_type == k) for k in sorted({r.req_type for r in reqs})},
        "risk_clauses": (t.extraction or {}).get("risk_clauses", [])[:10],
    }
    _track(db, state, "tender_intelligence", {"requirements": len(reqs)})
    return {"tender_brief": brief}


def company_intelligence(state: AnalysisState, config) -> dict:
    db = _db_from(config)
    ev, snap = load_company_evidence(db, uuid.UUID(state["organization_id"]))
    brief = {
        "certifications": len(ev.certifications),
        "financial_years": sorted({f.values["fiscal_year"] for f in ev.financials}),
        "projects": len(ev.projects),
        "registrations": sorted({f.values["kind"] for f in ev.registrations}),
        "documents": sum(len(v) for v in ev.documents_by_category.values()),
        "document_versions_in_snapshot": len(snap["document_versions"]),
    }
    _track(db, state, "company_intelligence", brief)
    return {"company_brief": brief}


def eligibility_analyst(state: AnalysisState, config) -> dict:
    db = _db_from(config)
    a = db.get(EligibilityAssessment, uuid.UUID(state["assessment_id"]))
    progress = config["configurable"].get("progress")
    result = elig.compute_decisions(db, a, progress)
    db.commit()
    _track(db, state, "eligibility_analyst", {"result": result, "overall": a.overall_status, "score": a.readiness_score})
    return {"eligibility": {"overall": a.overall_status, "score": a.readiness_score, "summary": a.summary}}


def risk_review(state: AnalysisState, config) -> dict:
    db = _db_from(config)
    a = db.get(EligibilityAssessment, uuid.UUID(state["assessment_id"]))
    t = db.get(Tender, a.tender_id)
    rows = db.execute(
        select(EligibilityDecision, TenderRequirement).join(TenderRequirement, TenderRequirement.id == EligibilityDecision.requirement_id)
        .where(EligibilityDecision.assessment_id == a.id)
    ).all()
    risks: list[dict[str, Any]] = []
    for d, r in rows:
        if d.machine_status == DecisionStatus.VERIFIED_NOT_SATISFIED and r.mandatory in ("mandatory", "conditional"):
            risks.append({"severity": "high", "kind": "mandatory_failure", "code": r.code, "text": d.explanation})
        elif d.machine_status == DecisionStatus.CONFLICTING_EVIDENCE:
            risks.append({"severity": "high", "kind": "conflicting_evidence", "code": r.code, "text": d.explanation})
        elif d.machine_status == DecisionStatus.INSUFFICIENT_EVIDENCE and r.mandatory == "mandatory":
            risks.append({"severity": "high", "kind": "missing_evidence", "code": r.code, "text": "; ".join(d.gaps) or d.explanation})
        if r.req_type == "emd" or "performance security" in r.source_text.lower():
            risks.append({"severity": "medium", "kind": "financial_commitment", "code": r.code, "text": "Financial commitment requires approval: " + r.title})
    for clause in (t.extraction or {}).get("risk_clauses", [])[:8]:
        risks.append({"severity": "medium", "kind": "contract_clause", "code": None, "text": clause})
    if t.submission_deadline and t.submission_deadline - utcnow() < timedelta(days=7):
        risks.append({"severity": "high", "kind": "deadline", "code": None, "text": f"Submission deadline in under 7 days ({t.submission_deadline.astimezone(IST):%d %b %H:%M} IST)."})
    narrative = _narrative(db, a, state, risks)
    a.summary = {**(a.summary or {}), "risks": risks, "narrative": narrative}
    db.commit()
    _track(db, state, "risk_review", {"risks": len(risks), "narrative_mode": narrative["mode"]})
    return {"risks": risks, "narrative": narrative}


def _narrative(db: Session, a: EligibilityAssessment, state: AnalysisState, risks: list[dict]) -> dict[str, Any]:
    s = a.summary or {}
    ms = s.get("mandatory_by_status", {})
    base = (
        f"{ms.get('verified_satisfied', 0)} mandatory condition(s) are supported by uploaded records; "
        f"{ms.get('verified_not_satisfied', 0)} failed; {ms.get('insufficient_evidence', 0)} lack evidence; "
        f"{ms.get('conflicting_evidence', 0)} have conflicting evidence; {ms.get('needs_human_review', 0)} need review."
    )
    llm = get_llm()
    if llm is None:
        return {"mode": "rule_based", "text": base}
    from pydantic import BaseModel

    class Narrative(BaseModel):
        summary: str
        top_actions: list[str]

    try:
        budget = RunBudget.default()
        prompt = (
            "Write a 3-sentence executive readiness summary and up to 4 next actions for a bid manager, using ONLY the "
            "rule-engine results below. Do not change or reinterpret any status.\n"
            f"<untrusted>\nCounts: {s}\nRisks: {risks[:12]}\n</untrusted>"
        )
        out = llm.structured(Narrative, prompt, db=db, org_id=a.organization_id, purpose="readiness_narrative", fast=True,
                             budget=budget, run_id=uuid.UUID(state["run_id"]), tender_id=a.tender_id, effort="low")
        return {"mode": "llm", "text": out.summary, "actions": out.top_actions, "facts": base}
    except (ServiceUnavailable, LLMRefused, QuotaExceeded) as exc:
        return {"mode": "rule_based", "text": base, "llm_error": str(exc)[:200]}


def human_review(state: AnalysisState, config) -> dict:
    db = _db_from(config)
    a = db.get(EligibilityAssessment, uuid.UUID(state["assessment_id"]))
    rows = db.execute(
        select(EligibilityDecision.id, TenderRequirement.code, TenderRequirement.title, EligibilityDecision.status)
        .join(TenderRequirement, TenderRequirement.id == EligibilityDecision.requirement_id)
        .where(EligibilityDecision.assessment_id == a.id, TenderRequirement.mandatory.in_(["mandatory", "conditional"]),
               EligibilityDecision.status.in_([DecisionStatus.NEEDS_HUMAN_REVIEW, DecisionStatus.CONFLICTING_EVIDENCE]),
               EligibilityDecision.review_state == "pending")
    ).all()
    open_conflicts = db.scalar(select(func.count()).select_from(EvidenceConflict).join(EligibilityDecision, EligibilityDecision.id == EvidenceConflict.decision_id)
                               .where(EligibilityDecision.assessment_id == a.id, EvidenceConflict.resolved_at.is_(None))) or 0
    items = [{"decision_id": str(i), "code": c, "title": t, "status": s} for i, c, t, s in rows]
    if not items and not open_conflicts:
        # Also reached when a paused run is resumed after reviewers resolved everything
        # (LangGraph re-executes the interrupted node from its start).
        _track(db, state, "human_review", {"pending": 0, "resolved_before_resume": a.status == AssessmentStatus.AWAITING_REVIEW})
        return {"review_items": [], "review_outcome": {"required": False}}
    a.status = AssessmentStatus.AWAITING_REVIEW
    run = db.get(AgentRun, uuid.UUID(state["run_id"]))
    if run:
        run.status, run.interrupt_payload = "interrupted", {"pending": items, "open_conflicts": open_conflicts}
    db.commit()
    # Pauses the graph; resumed via resume_tender_analysis() once a reviewer signs off.
    answer = interrupt({"type": "eligibility_review", "pending": items, "open_conflicts": open_conflicts})
    if run:
        run.status, run.interrupt_payload = "running", None
    db.commit()
    _track(db, state, "human_review", {"reviewer": answer.get("reviewer_id"), "note": answer.get("note")})
    return {"review_items": items, "review_outcome": {"required": True, **answer}}


def remediation(state: AnalysisState, config) -> dict:
    """Create one remediation task per gap (dedup keys make this safe to re-run)."""
    db = _db_from(config)
    a = db.get(EligibilityAssessment, uuid.UUID(state["assessment_id"]))
    ws = db.scalar(select(BidWorkspace).where(BidWorkspace.tender_id == a.tender_id))
    created = 0
    if ws:
        created = create_remediation_tasks(db, a, ws)
        db.commit()
    _track(db, state, "remediation", {"tasks_created": created, "workspace": bool(ws)})
    return {"remediation": {"tasks_created": created}}


def create_remediation_tasks(db: Session, a: EligibilityAssessment, ws: BidWorkspace, created_by: uuid.UUID | None = None) -> int:
    rows = db.execute(
        select(EligibilityDecision, TenderRequirement).join(TenderRequirement, TenderRequirement.id == EligibilityDecision.requirement_id)
        .where(EligibilityDecision.assessment_id == a.id, EligibilityDecision.status.notin_([DecisionStatus.VERIFIED_SATISFIED, DecisionStatus.NOT_APPLICABLE]))
    ).all()
    created = 0
    for d, r in rows:
        for gap in d.gaps:
            key = f"remediation:{r.tender_id}:{r.code}:{hashlib.sha256(gap.encode()).hexdigest()[:16]}"
            if db.scalar(select(BidTask.id).where(BidTask.workspace_id == ws.id, BidTask.dedup_key == key)):
                continue
            db.add(BidTask(organization_id=ws.organization_id, workspace_id=ws.id, title=gap[:300], description=f"{r.code}: {r.title}\n\n{d.explanation}",
                           priority="high" if d.risk_level == "high" else "medium", requirement_id=r.id, source="remediation", dedup_key=key,
                           created_by_id=created_by))
            created += 1
    return created


def finalize_node(state: AnalysisState, config) -> dict:
    db = _db_from(config)
    a = db.get(EligibilityAssessment, uuid.UUID(state["assessment_id"]))
    elig.finalize(db, a)
    run = db.get(AgentRun, uuid.UUID(state["run_id"]))
    if run:
        run.status, run.finished_at = "completed", utcnow()
        run.result = {"overall": a.overall_status, "score": a.readiness_score}
    db.commit()
    return {}


def build_graph() -> StateGraph:
    g = StateGraph(AnalysisState)
    for name, fn in [("tender_intelligence", tender_intelligence), ("company_intelligence", company_intelligence),
                     ("eligibility_analyst", eligibility_analyst), ("risk_review", risk_review), ("human_review", human_review),
                     ("remediation", remediation), ("finalize", finalize_node)]:
        g.add_node(name, fn)
    g.add_edge(START, "tender_intelligence")
    g.add_edge("tender_intelligence", "company_intelligence")
    g.add_edge("company_intelligence", "eligibility_analyst")
    g.add_edge("eligibility_analyst", "risk_review")
    g.add_edge("risk_review", "human_review")
    g.add_edge("human_review", "remediation")
    g.add_edge("remediation", "finalize")
    g.add_edge("finalize", END)
    return g


def _config(db: Session, thread_id: str, progress: Callable | None = None) -> dict:
    return {"configurable": {"thread_id": thread_id, "db": db, "progress": progress}, "recursion_limit": get_settings().agent_max_steps}


def run_tender_analysis(db: Session, a: EligibilityAssessment, progress: Callable[[int, str], None] | None = None) -> dict:
    run = db.get(AgentRun, a.agent_run_id) if a.agent_run_id else None
    if run is None:
        run = AgentRun(organization_id=a.organization_id, kind="tender_analysis", thread_id=f"assessment:{a.id}", subject_type="eligibility_assessment",
                       subject_id=a.id, started_by_id=a.triggered_by_id, mode="llm" if get_settings().llm_configured else "rules")
        db.add(run)
        db.flush()
        a.agent_run_id = run.id
        db.commit()
    state: AnalysisState = {"assessment_id": str(a.id), "organization_id": str(a.organization_id), "tender_id": str(a.tender_id), "run_id": str(run.id)}
    try:
        with checkpointer() as saver:
            graph = build_graph().compile(checkpointer=saver)
            cfg = _config(db, run.thread_id, progress)
            existing = graph.get_state(cfg)
            out = graph.invoke(None if existing.next else state, cfg)  # resume after a crash, else start
    except RunCancelled:
        a.status = AssessmentStatus.FAILED
        return {"cancelled": True}
    except Exception as exc:
        db.rollback()
        run = db.get(AgentRun, run.id)
        run.status, run.error, run.finished_at = "failed", str(exc)[:2000], utcnow()
        a = db.get(EligibilityAssessment, a.id)
        a.status = AssessmentStatus.FAILED
        db.commit()
        raise
    interrupted = bool(out.get("__interrupt__"))
    return {"interrupted": interrupted, "overall": a.overall_status, "score": a.readiness_score}


def resume_tender_analysis(db: Session, a: EligibilityAssessment, reviewer_id: uuid.UUID, note: str | None) -> dict:
    """Human-in-the-loop resume using the persisted thread id."""
    run = db.get(AgentRun, a.agent_run_id) if a.agent_run_id else None
    if run is None or run.status != "interrupted":
        raise ServiceUnavailable("This assessment is not waiting for review.")
    with checkpointer() as saver:
        graph = build_graph().compile(checkpointer=saver)
        out = graph.invoke(Command(resume={"reviewer_id": str(reviewer_id), "note": note, "approved_at": utcnow().isoformat()}),
                           _config(db, run.thread_id))
    return {"interrupted": bool(out.get("__interrupt__")), "overall": a.overall_status}

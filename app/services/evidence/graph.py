"""Requirements-to-Evidence graph projection (persisted links → nodes/edges)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.context import TenantContext
from app.db.models import Document
from app.services.eligibility import service as elig

STATUS_COLOR = {
    "verified_satisfied": "#1E8E5A",
    "verified_not_satisfied": "#C0392B",
    "insufficient_evidence": "#D4A017",
    "conflicting_evidence": "#E67E22",
    "needs_human_review": "#2E86C1",
    "not_applicable": "#8395A7",
}
RELATION_STYLE = {"supports": "solid", "partial": "dashed", "related": "dotted", "contradicts": "solid"}


def build(db: Session, ctx: TenantContext, assessment_id: uuid.UUID) -> dict[str, Any]:
    rep = elig.report(db, ctx, assessment_id)
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    doc_ids = {lk.document_id for it in rep["items"] for lk in it["links"] if lk.document_id}
    docs = {d.id: d for d in db.scalars(select(Document).where(Document.id.in_(doc_ids), Document.organization_id == ctx.organization_id))}
    for it in rep["items"]:
        r, d = it["requirement"], it["decision"]
        rid = f"req:{r.id}"
        nodes[rid] = {"id": rid, "kind": "requirement", "label": f"{r.code} {r.title[:60]}", "status": d.status, "mandatory": r.mandatory,
                      "color": STATUS_COLOR.get(d.status, "#888"), "decision_id": str(d.id), "requirement_id": str(r.id)}
        if not it["links"]:
            gid = f"gap:{r.id}"
            nodes[gid] = {"id": gid, "kind": "gap", "label": "No evidence", "color": "#D4A017"}
            edges.append({"source": rid, "target": gid, "relation": "missing", "style": "dashed", "color": "#D4A017"})
        for lk in it["links"]:
            if not lk.document_id:
                continue
            did = f"doc:{lk.document_id}"
            doc = docs.get(lk.document_id)
            nodes.setdefault(did, {"id": did, "kind": "document", "label": (doc.title if doc else lk.label or "document")[:60],
                                   "category": doc.category if doc else None, "color": "#1F2A44", "document_id": str(lk.document_id)})
            edges.append({"source": rid, "target": did, "relation": lk.relation, "origin": lk.origin, "page": lk.page_no, "confidence": lk.confidence,
                          "link_id": str(lk.id), "version_id": str(lk.document_version_id) if lk.document_version_id else None,
                          "style": RELATION_STYLE.get(lk.relation, "solid"), "color": "#C0392B" if lk.relation == "contradicts" else "#5B6B7F"})
    return {"assessment_id": str(assessment_id), "nodes": list(nodes.values()), "edges": edges, "legend": STATUS_COLOR}

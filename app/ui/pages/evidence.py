"""Eligibility intelligence, assessment report & review, evidence graph, document viewer."""

from __future__ import annotations

from fastapi import Request
from nicegui import ui
from sqlalchemy import select

from app.auth.deps import parse_uuid
from app.auth.permissions import Perm
from app.core.errors import AppError
from app.db.models import DocumentPage, EligibilityAssessment, Tender, TenderRequirement
from app.db.session import session_scope
from app.extraction.normalization.values import IST
from app.services.documents import service as documents
from app.services.eligibility import service as elig
from app.services.evidence import graph as evgraph
from app.ui.components.common import download_button
from app.ui.layouts.shell import run, shell
from app.ui.themes.theme import (
    STATUS,
    chart,
    empty_state,
    overall_badge,
    page_header,
    panel,
    score_ring,
    status_badge,
)


@ui.page("/eligibility")
def eligibility_hub(request: Request):
    with shell(request, "/eligibility") as pc:
        if not pc:
            return
        with page_header("Eligibility Intelligence", "Latest requirement-by-requirement assessments across your tenders."):
            ui.button("Evidence graphs", icon="hub", on_click=lambda: ui.navigate.to("/evidence")).props("outline color=primary")
        with session_scope() as db:
            rows = db.execute(select(EligibilityAssessment, Tender).join(Tender, Tender.id == EligibilityAssessment.tender_id)
                              .where(EligibilityAssessment.organization_id == pc.ctx.organization_id, EligibilityAssessment.status != "superseded")
                              .order_by(EligibilityAssessment.created_at.desc())).all()
            data = [(a.id, t.id, t.title, a.run_no, a.status, a.overall_status, a.readiness_score, a.summary or {}) for a, t in rows]
        if not data:
            with ui.card().classes("tp-card w-full").props("flat"):
                empty_state("fact_check", "No assessments yet", "Open a tender and run the eligibility check.", "Go to tenders", lambda: ui.navigate.to("/tenders"))
            return
        for aid, tid, title, run_no, status, overall, score, summary in data:
            with ui.card().classes("tp-card tp-panel w-full").props("flat"):
                with ui.row().classes("w-full items-center justify-between no-wrap gap-4"):
                    with ui.row().classes("items-center no-wrap gap-4 col").style("min-width:0"):
                        with ui.column().classes("items-center gap-0"):
                            score_ring(score)
                            ui.label("readiness").classes("tp-caption")
                        with ui.column().classes("gap-1 col").style("min-width:0"):
                            ui.link(title, f"/tenders/{tid}").classes("tp-link text-subtitle1").style("color:#101828")
                            with ui.row().classes("items-center gap-2"):
                                overall_badge(overall)
                                ui.label(f"Run #{run_no} · {status.replace('_', ' ')}").classes("tp-caption")
                                if summary.get("pending_reviews"):
                                    ui.badge(f"{summary['pending_reviews']} awaiting review", color="info").classes("tp-chip")
                            ms = summary.get("mandatory_by_status", {})
                            with ui.row().classes("gap-1 q-mt-xs"):
                                for k, n in ms.items():
                                    ui.badge(f"{STATUS.get(k, (k,))[0]}: {n}", color=STATUS.get(k, ("", "grey"))[1]).props("outline").classes("tp-chip")
                    ui.button("Open report", on_click=lambda aid=aid: ui.navigate.to(f"/assessments/{aid}")).props("unelevated color=primary icon-right=arrow_forward")


@ui.page("/assessments/{assessment_id}")
def assessment_page(request: Request, assessment_id: str):
    aid = parse_uuid(assessment_id)
    with shell(request, "/eligibility") as pc:
        if not pc:
            return

        @ui.refreshable
        def body():
            try:
                with session_scope() as db:
                    rep = elig.report(db, pc.ctx, aid)
                    a = rep["assessment"]
                    t = db.get(Tender, a.tender_id)
                    head = (t.id, t.title, a.run_no, a.status, a.overall_status, a.readiness_score, a.reference_date, a.summary or {}, a.tender_version_no)
                    items = [{
                        "d_id": it["decision"].id, "code": it["requirement"].code, "title": it["requirement"].title, "mandatory": it["requirement"].mandatory,
                        "status": it["decision"].status, "machine": it["decision"].machine_status, "explanation": it["decision"].explanation,
                        "gaps": it["decision"].gaps, "risk": it["decision"].risk_level, "review_state": it["decision"].review_state, "note": it["decision"].reviewer_note,
                        "computation": it["decision"].computation, "clause": it["requirement"].source_text, "page": it["requirement"].source_page,
                        "req_vid": it["requirement"].source_document_version_id, "req_id": it["requirement"].id,
                        "links": [(lk.label, lk.page_no, lk.relation, lk.origin, lk.excerpt, lk.document_version_id, lk.id, lk.confidence) for lk in it["links"]],
                        "conflicts": [(c.id, c.description, c.resolved_at) for c in it["conflicts"]],
                        "reviews": [(e.action, e.to_value, e.note, e.created_at) for e in it["reviews"]],
                    } for it in rep["items"]]
                    missing = rep["missing_documents"]
            except AppError as exc:
                ui.label(exc.message).classes("text-negative")
                return
            tid, title, run_no, status, overall, score, ref, summary, tver = head
            with page_header(f"Eligibility assessment · run #{run_no}", f"Reference date {ref.astimezone(IST):%d %b %Y} (submission deadline) · tender version {tver} · {status.replace('_', ' ')}",
                             back=(title, f"/tenders/{tid}")):
                    ui.button("Evidence graph", icon="hub", on_click=lambda: ui.navigate.to(f"/evidence/{aid}")).props("outline color=primary")
                    if status == "awaiting_review" and pc.can(Perm.ELIGIBILITY_REVIEW):
                        def complete():
                            def do(db):
                                from app.ai.workflows.tender_analysis import resume_tender_analysis

                                return resume_tender_analysis(db, elig.get_assessment(db, pc.ctx, aid), pc.ctx.user_id, "Review completed in UI")
                            if run(do, success="Review recorded — workflow resumed"):
                                body.refresh()
                        ui.button("Complete review & resume", icon="play_arrow", on_click=complete).props("unelevated color=primary")
            with ui.element("div").classes("w-full").style("display:grid;grid-template-columns:repeat(auto-fit,minmax(min(280px,100%),1fr));gap:16px;align-items:stretch"):
                with ui.card().classes("tp-card tp-panel").props("flat"):
                    ui.label("Overall result").classes("tp-caption text-weight-medium")
                    with ui.row().classes("items-center no-wrap gap-4"):
                        score_ring(score, large=True)
                        with ui.column().classes("gap-1"):
                            overall_badge(overall)
                            ui.label("readiness score").classes("tp-caption")
                    ui.label("Readiness is explanatory; a failed or unresolved mandatory condition always governs.").classes("tp-caption")
                with panel("Summary", icon="summarize"):
                    narrative = summary.get("narrative") or {}
                    if narrative:
                        ui.label(narrative.get("text", "")).classes("text-body2")
                        ui.label("AI summary of rule results" if narrative.get("mode") == "llm" else "Rule-based summary").classes("text-caption tp-muted")
                    with ui.row().classes("gap-1 q-mt-sm"):
                        for k, n in (summary.get("by_status") or {}).items():
                            ui.badge(f"{STATUS.get(k, (k,))[0]}: {n}", color=STATUS.get(k, ("", "grey"))[1]).props("outline").classes("tp-chip")
                with panel("Missing / to prepare", icon="playlist_add_check"):
                    if not missing:
                        with ui.row().classes("items-center gap-2"):
                            ui.icon("check_circle", size="18px").style("color:#079455")
                            ui.label("Nothing missing.").classes("text-body2")
                    for m in missing:
                        with ui.row().classes("items-start no-wrap gap-2"):
                            ui.icon("radio_button_unchecked", size="16px").style("color:#F79009;margin-top:2px")
                            ui.label(m).classes("text-body2")
            if summary.get("risks"):
                with ui.expansion(f"Risk review ({len(summary['risks'])} flags)", icon="warning_amber").classes("tp-card w-full"):
                    for r in summary["risks"]:
                        with ui.row().classes("items-start no-wrap gap-2 q-px-md q-py-xs"):
                            ui.badge(r["severity"].capitalize(), color={"high": "negative", "medium": "warning"}.get(r["severity"], "grey-7")).classes("tp-chip")
                            ui.label(f"{r['kind'].replace('_', ' ').capitalize()} {r.get('code') or ''}: {r['text'][:300]}").classes("text-body2")
            ui.label(f"Requirements ({len(items)})").classes("tp-h2 q-mt-sm")
            for it in items:
                accent = {"positive": "#12B76A", "negative": "#F04438", "warning": "#F79009", "orange": "#EF6820", "info": "#2E90FA"}.get(STATUS.get(it["status"], ("", "grey"))[1], "#D0D5DD")
                with ui.card().classes("tp-card tp-panel w-full").props("flat").style(f"border-left:3px solid {accent}"):
                    with ui.row().classes("w-full items-start justify-between no-wrap"):
                        with ui.column().classes("gap-1 col"):
                            with ui.row().classes("items-center gap-2"):
                                ui.label(it["code"]).classes("tp-num").style("font-weight:650;color:#475467;font-size:.8rem;background:#F2F4F7;border-radius:6px;padding:1px 7px")
                                status_badge(it["status"])
                                if it["status"] != it["machine"]:
                                    ui.badge(f"Rule engine: {it['machine'].replace('_', ' ')}", color="grey-7").props("outline").classes("tp-chip").tooltip("Overridden by a reviewer; the machine result is preserved.")
                                ui.badge(it["mandatory"].capitalize(), color="negative" if it["mandatory"] == "mandatory" else "orange" if it["mandatory"] == "conditional" else "grey-6").props("outline").classes("tp-chip")
                                ui.badge(f"Risk {it['risk']}", color="grey-7").props("outline").classes("tp-chip")
                                if it["review_state"] != "pending":
                                    ui.badge(it["review_state"].capitalize(), color="positive").props("outline").classes("tp-chip")
                            ui.label(it["title"]).classes("text-subtitle1").style("color:#101828")
                            ui.label(it["explanation"]).classes("text-body2")
                            for g in it["gaps"]:
                                with ui.row().classes("items-start no-wrap gap-1"):
                                    ui.icon("subdirectory_arrow_right", size="15px").style("color:#DC6803;margin-top:1px")
                                    ui.label(g).classes("text-caption").style("color:#B54708")
                            for cid, desc, resolved in it["conflicts"]:
                                with ui.row().classes("items-center gap-2"):
                                    ui.icon("report", color="orange")
                                    ui.label(desc[:300]).classes("text-caption")
                                    if not resolved and pc.can(Perm.ELIGIBILITY_REVIEW):
                                        ui.button("Mark resolved", on_click=lambda cid=cid: resolve(cid)).props("dense flat color=orange")
                                    elif resolved:
                                        ui.badge("resolved", color="positive").props("outline")
                        if pc.can(Perm.ELIGIBILITY_REVIEW):
                            with ui.column().classes("gap-1").style("min-width:100px"):
                                ui.button("Accept", icon="check", on_click=lambda d=it["d_id"]: run(lambda db: elig.review_decision(db, pc.ctx, d, action="accept"),
                                                                                       success="Accepted", refresh=body.refresh)).props("dense unelevated color=positive")
                                ui.button("Override", on_click=lambda it=it: override(it)).props("dense flat")
                                ui.button("Annotate", on_click=lambda it=it: annotate(it)).props("dense flat")
                    with ui.expansion("Evidence, tender clause & computation", icon="link").classes("w-full").style("background:#F9FAFB;border:1px solid #EAECF0"):
                        with ui.row().classes("items-center gap-2"):
                            ui.label(f"Tender clause (p.{it['page']}):").classes("text-caption tp-muted")
                            if it["req_vid"]:
                                ui.link("open in tender", f"/viewer/{it['req_vid']}?page={it['page']}&req={it['req_id']}").classes("tp-link text-caption")
                        ui.label(it["clause"]).classes("tp-quote")
                        if not it["links"]:
                            ui.label("No supporting evidence found in the document vault.").classes("text-caption text-warning")
                        for label, page, relation, origin, excerpt, vid, lid, conf in it["links"]:
                            with ui.row().classes("w-full items-start gap-2 no-wrap q-mt-xs"):
                                ui.badge(relation.capitalize(), color={"supports": "positive", "contradicts": "negative", "partial": "info"}.get(relation, "grey-6")).classes("tp-chip")
                                with ui.column().classes("gap-0 col"):
                                    with ui.row().classes("items-center gap-2"):
                                        ui.label(f"{label} · p.{page or '–'} · {origin} · confidence {conf:.0%}").classes("text-caption text-weight-medium")
                                        if vid:
                                            ui.link("open source", f"/viewer/{vid}?page={page or 1}&link={lid}").classes("tp-link text-caption")
                                    if excerpt:
                                        ui.label(excerpt[:500]).classes("tp-quote")
                        if it["computation"]:
                            ui.label("Rule computation").classes("text-caption tp-muted q-mt-sm")
                            ui.code(_fmt(it["computation"]), language="json").classes("w-full")
                        for action, to_value, note, at in it["reviews"]:
                            ui.label(f"{at.astimezone(IST):%d %b %H:%M} — {action} → {to_value}: {note or ''}").classes("text-caption tp-muted")

        def resolve(cid):
            with ui.dialog() as dlg, ui.card().style("width:480px"):
                ui.label("Resolve conflict").classes("tp-h1").style("font-size:1.2rem")
                note = ui.textarea("Which source is authoritative, and why?").props("outlined").classes("w-full")
                ui.button("Save", on_click=lambda: (run(lambda db: elig.resolve_conflict(db, pc.ctx, cid, note.value or "Resolved"), success="Resolved", refresh=body.refresh), dlg.close())).props("unelevated color=primary")
            dlg.open()

        def override(it):
            with ui.dialog() as dlg, ui.card().style("width:520px"):
                ui.label(f"Override {it['code']}").classes("tp-h1").style("font-size:1.2rem")
                ui.label(f"Rule engine result: {it['machine'].replace('_', ' ')}. Overrides are audited; the machine result is kept.").classes("text-caption tp-muted")
                new = ui.select({k: v[0] for k, v in STATUS.items()}, value=it["status"], label="New status").props("outlined").classes("w-full")
                note = ui.textarea("Justification (required)").props("outlined").classes("w-full")

                def save():
                    if run(lambda db: elig.review_decision(db, pc.ctx, it["d_id"], action="override", new_status=new.value, note=note.value), success="Override recorded", refresh=body.refresh):
                        dlg.close()
                ui.button("Save override", on_click=save).props("unelevated color=primary")
            dlg.open()

        def annotate(it):
            with ui.dialog() as dlg, ui.card().style("width:480px"):
                ui.label(f"Annotate {it['code']}").classes("tp-h1").style("font-size:1.2rem")
                note = ui.textarea("Note").props("outlined").classes("w-full")
                ui.button("Save", on_click=lambda: (run(lambda db: elig.review_decision(db, pc.ctx, it["d_id"], action="annotate", note=note.value), success="Saved", refresh=body.refresh), dlg.close())).props("unelevated color=primary")
            dlg.open()

        body()


def _fmt(obj) -> str:
    import json

    return json.dumps(obj, indent=2, default=str)[:3000]


@ui.page("/evidence")
def evidence_index(request: Request):
    with shell(request, "/evidence") as pc:
        if not pc:
            return
        with page_header("Evidence Graph", "Requirements-to-evidence maps for each eligibility assessment."):
            pass
        with session_scope() as db:
            rows = db.execute(select(EligibilityAssessment.id, Tender.title, EligibilityAssessment.run_no).join(Tender, Tender.id == EligibilityAssessment.tender_id)
                              .where(EligibilityAssessment.organization_id == pc.ctx.organization_id, EligibilityAssessment.status != "superseded")).all()
        if not rows:
            with ui.card().classes("tp-card w-full").props("flat"):
                empty_state("hub", "No graphs yet", "Run an eligibility assessment to build the requirements-to-evidence graph.")
            return
        with ui.element("div").classes("tp-grid-3 w-full"):
            for aid, title, run_no in rows:
                with ui.element("a").props(f'href="/evidence/{aid}"').classes("tp-card tp-hover column gap-2").style("padding:18px;text-decoration:none"):
                    ui.icon("hub").classes("tp-feature-icon tp-tone-violet").style("width:38px;height:38px;font-size:20px")
                    ui.label(title).classes("tp-h2")
                    with ui.row().classes("items-center gap-1 tp-caption"):
                        ui.label(f"Run #{run_no}")
                        ui.label("· Open graph")
                        ui.icon("arrow_forward", size="14px")


@ui.page("/evidence/{assessment_id}")
def evidence_graph(request: Request, assessment_id: str):
    aid = parse_uuid(assessment_id)
    with shell(request, "/evidence") as pc:
        if not pc:
            return
        try:
            with session_scope() as db:
                g = evgraph.build(db, pc.ctx, aid)
                a = elig.get_assessment(db, pc.ctx, aid)
                title = db.get(Tender, a.tender_id).title
        except AppError as exc:
            ui.label(exc.message).classes("text-negative")
            return
        with page_header(f"Evidence graph · {title}", "Requirements (coloured by status) on the left, your documents on the right. Click an edge to open the cited page; scroll to zoom.",
                         back=("Assessment report", f"/assessments/{aid}")):
            pass
        with ui.row().classes("gap-2"):
            for k, (label, color, _icon) in STATUS.items():
                ui.badge(label, color=color).classes("tp-chip")
        reqs = [n for n in g["nodes"] if n["kind"] == "requirement"]
        others = [n for n in g["nodes"] if n["kind"] != "requirement"]
        step_r = 760 / max(len(reqs), 1)
        step_o = 760 / max(len(others), 1)
        nodes = []
        for i, n in enumerate(reqs):
            nodes.append({"id": n["id"], "name": n["label"], "x": 0, "y": i * step_r, "symbolSize": 18 if n["mandatory"] == "mandatory" else 12,
                          "itemStyle": {"color": n["color"]}, "label": {"show": True, "position": "left", "fontSize": 11}})
        for i, n in enumerate(others):
            nodes.append({"id": n["id"], "name": n["label"], "x": 600, "y": i * step_o, "symbolSize": 16, "symbol": "roundRect" if n["kind"] == "document" else "triangle",
                          "itemStyle": {"color": n["color"]}, "label": {"show": True, "position": "right", "fontSize": 11}})
        links = [{"source": e["source"], "target": e["target"], "value": e["relation"], "lineStyle": {"type": e["style"], "color": e["color"], "width": 2 if e["relation"] == "supports" else 1.2},
                  "label": {"show": False, "formatter": e["relation"]}} for e in g["edges"]]
        edge_lookup = {f"{e['source']}>{e['target']}": e for e in g["edges"]}
        graph = chart({"tooltip": {"formatter": "{b}"}, "series": [{"type": "graph", "layout": "none", "roam": True, "left": "30%", "right": "22%", "top": 30, "bottom": 30, "data": nodes, "links": links,
                                                                     "edgeSymbol": ["none", "arrow"], "emphasis": {"focus": "adjacency"}}]},
                          on_click=lambda e: _edge_click(e, edge_lookup)).classes("tp-card w-full").style("height: 820px")
        _ = graph


def _edge_click(e, lookup):
    data = getattr(e, "data", None) or {}
    if isinstance(data, dict) and data.get("source"):
        edge = lookup.get(f"{data['source']}>{data['target']}")
        if edge and edge.get("version_id"):
            ui.navigate.to(f"/viewer/{edge['version_id']}?page={edge.get('page') or 1}&link={edge['link_id']}")
        elif edge:
            ui.notify(f"{edge['relation']} ({edge['origin']})")


@ui.page("/viewer/{version_id}")
def viewer(request: Request, version_id: str):
    vid = parse_uuid(version_id)
    page_no = int(request.query_params.get("page", "1") or 1)
    link_id, req_id = request.query_params.get("link"), request.query_params.get("req")
    with shell(request, "/vault") as pc:
        if not pc:
            return
        try:
            with session_scope() as db:
                ver = documents.get_version(db, pc.ctx, vid)
                doc = documents.get_document(db, pc.ctx, ver.document_id)
                pg = db.scalar(select(DocumentPage).where(DocumentPage.version_id == vid, DocumentPage.page_no == page_no))
                pages_total = ver.page_count or 1
                bbox, excerpt = None, None
                if link_id:
                    from app.db.models import RequirementEvidenceLink

                    lk = db.scalar(select(RequirementEvidenceLink).where(RequirementEvidenceLink.id == parse_uuid(link_id), RequirementEvidenceLink.organization_id == pc.ctx.organization_id))
                    if lk:
                        bbox, excerpt = lk.bbox, lk.excerpt
                if req_id:
                    rq = db.scalar(select(TenderRequirement).where(TenderRequirement.id == parse_uuid(req_id), TenderRequirement.organization_id == pc.ctx.organization_id))
                    if rq:
                        bbox, excerpt = rq.source_bbox, rq.source_text
                info = (doc.title, ver.version_no, ver.original_filename, ver.content_type, ver.processing_status, ver.extraction_method, ver.quality_notes,
                        pg.text if pg else "", pg.width if pg else None, pg.height if pg else None, pg.method if pg else None)
                if excerpt and not bbox and pg:
                    probe = " ".join(excerpt.split())[:50]
                    for b in pg.blocks or []:
                        if probe[:35] and probe[:35] in " ".join(b["text"].split()):
                            bbox = b.get("bbox")
                            break
        except AppError as exc:
            ui.label(exc.message).classes("text-negative")
            return
        title, vno, fname, ctype, status, method, notes, text, w, h, pmethod = info
        with page_header(title, f"{fname} · version {vno} · {method} · {status}", back=("Document vault", "/vault")):
            with ui.row().classes("items-center no-wrap gap-0 tp-card").style("padding:2px;border-radius:10px"):
                ui.button(icon="chevron_left", on_click=lambda: ui.navigate.to(f"/viewer/{vid}?page={page_no - 1}")).props("flat dense").set_enabled(page_no > 1)
                ui.label(f"Page {page_no} of {pages_total}").classes("text-weight-medium tp-num q-px-sm").style("color:#101828;font-size:.85rem")
                ui.button(icon="chevron_right", on_click=lambda: ui.navigate.to(f"/viewer/{vid}?page={page_no + 1}")).props("flat dense").set_enabled(page_no < pages_total)
            download_button(pc, vid, props="outline color=primary")
        for n in notes or []:
            ui.label(n).classes("text-caption text-warning")
        with ui.splitter(value=58).classes("w-full tp-card").style("min-height: 80vh;overflow:hidden") as split:
            with split.before, ui.element("div").style("background:#F2F4F7;padding:24px;min-height:100%;overflow:auto;text-align:center"):
                if ctype == "application/pdf":
                    zoom = 1.5
                    with ui.element("div").style("position: relative; display: inline-block"):
                        ui.element("img").props(f'src="/api/v1/document-versions/{vid}/pages/{page_no}/image" alt="Page {page_no} of {title}"').style(f"width: {int((w or 595) * zoom)}px; display:block; border-radius:4px; box-shadow:0 4px 16px -4px rgba(16,24,40,.18), 0 0 0 1px rgba(16,24,40,.06)")
                        if bbox and w:
                            x0, y0, x1, y1 = bbox
                            ui.element("div").classes("tp-hl").style(f"left:{x0 * zoom - 3}px; top:{y0 * zoom - 3}px; width:{(x1 - x0) * zoom + 6}px; height:{(y1 - y0) * zoom + 6}px")
                    if excerpt and not bbox:
                        ui.label("Exact coordinates unavailable for this passage — showing the cited page; the extracted text is on the right.").classes("text-caption text-warning")
                else:
                    ui.label("Preview is available for PDFs; showing extracted text instead.").classes("tp-muted")
            with split.after:
                with ui.column().classes("w-full gap-2").style("padding:20px 24px"):
                    if excerpt:
                        ui.label("Cited passage").classes("tp-h2")
                        ui.label(excerpt).classes("tp-quote w-full")
                    ui.label(f"Extracted text (page {page_no}, {pmethod or 'text'})").classes("tp-h2 q-mt-md")
                    ui.label(text or "No text extracted for this page.").classes("text-body2").style("white-space: pre-wrap;color:#475467")

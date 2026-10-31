"""Tender list, creation/import dialogs and tender detail."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from fastapi import Request
from nicegui import ui
from sqlalchemy import select

from app.auth.deps import parse_uuid
from app.auth.permissions import Perm
from app.core.errors import AppError
from app.db.models import BidWorkspace, TenderStatus
from app.db.session import session_scope
from app.extraction.normalization.values import IST, format_inr, format_inr_short
from app.integrations.sources import SOURCES
from app.services.eligibility import service as elig
from app.services.tenders import lifecycle
from app.services.tenders import service as tenders
from app.ui.components.common import download_button, job_tracker, upload_box
from app.ui.layouts.shell import run, shell
from app.ui.themes.theme import empty_state, overall_badge


def _tab_name(value) -> str:
    return value if isinstance(value, str) else getattr(value, "props", {}).get("name", "overview")


STATUS_COLOR = {"ready_for_review": "info", "processing": "warning", "shortlisted": "secondary", "bid_preparation": "primary", "won": "positive", "lost": "negative",
                "awaiting_approval": "orange", "ready_for_external_submission": "positive"}


def _dt(v: datetime | None) -> str:
    return v.astimezone(IST).strftime("%d %b %Y, %H:%M IST") if v else "—"


@ui.page("/tenders")
def tender_list(request: Request):
    with shell(request, "/tenders") as pc:
        if not pc:
            return
        state = {"q": "", "status": None, "sort": "deadline", "page": 0}
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("My Tenders").classes("tp-h1")
            if pc.can(Perm.TENDERS_WRITE):
                ui.button("Add tender", icon="add", on_click=lambda: new_dialog.open()).props("unelevated color=secondary")
        with ui.row().classes("w-full gap-2 items-center"):
            q = ui.input(placeholder="Search title, reference, organization").props("outlined dense clearable").classes("col")
            st = ui.select({None: "All statuses", **{s.value: s.value.replace("_", " ") for s in TenderStatus}}, value=None).props("outlined dense").style("width:220px")
            so = ui.select({"deadline": "Deadline", "value": "Value", "newest": "Newest", "title": "Title"}, value="deadline").props("outlined dense").style("width:160px")

        @ui.refreshable
        def table():
            with session_scope() as db:
                rows, total = tenders.list_tenders(db, pc.ctx, q=state["q"] or None, status=[state["status"]] if state["status"] else None,
                                                   sort=state["sort"], offset=state["page"] * 20, limit=20)
                data = [{"id": str(t.id), "title": t.title, "ref": t.reference_number or "—", "org": t.procuring_organization or "—", "status": t.status,
                         "value": format_inr_short(t.estimated_value) if t.estimated_value else "—", "deadline": _dt(t.submission_deadline)} for t in rows]
            if not data and not state["q"] and not state["status"]:
                empty_state("description", "No tenders yet", "Upload a tender document (PDF/DOCX/ZIP) or enter one manually.", "Add tender", lambda: new_dialog.open())
                return
            cols = [{"name": "title", "label": "Tender", "field": "title", "align": "left"}, {"name": "ref", "label": "Reference", "field": "ref", "align": "left"},
                    {"name": "org", "label": "Procuring entity", "field": "org", "align": "left"}, {"name": "value", "label": "Est. value", "field": "value"},
                    {"name": "deadline", "label": "Submission", "field": "deadline"}, {"name": "status", "label": "Status", "field": "status"}]
            t = ui.table(columns=cols, rows=data, row_key="id").props("flat").classes("w-full tp-card")
            t.add_slot("body-cell-title", '<q-td :props="props"><a class="tp-link" :href="\'/tenders/\' + props.row.id">{{ props.value }}</a></q-td>')
            t.add_slot("body-cell-status", '<q-td :props="props"><q-badge outline color="primary" :label="props.value.replaceAll(\'_\',\' \')" /></q-td>')
            with ui.row().classes("w-full justify-between items-center"):
                ui.label(f"{total} tender(s)").classes("tp-muted")
                with ui.row():
                    ui.button(icon="chevron_left", on_click=lambda: (state.update(page=max(0, state["page"] - 1)), table.refresh())).props("flat dense").set_enabled(state["page"] > 0)
                    ui.button(icon="chevron_right", on_click=lambda: (state.update(page=state["page"] + 1), table.refresh())).props("flat dense").set_enabled((state["page"] + 1) * 20 < total)

        q.on("update:model-value", lambda e: (state.update(q=q.value, page=0), table.refresh()), throttle=0.4)
        st.on_value_change(lambda e: (state.update(status=e.value, page=0), table.refresh()))
        so.on_value_change(lambda e: (state.update(sort=e.value), table.refresh()))
        table()

        with ui.dialog() as new_dialog, ui.card().classes("q-pa-lg").style("width: 720px; max-width: 96vw"):
            ui.label("Add a tender").classes("tp-h1")
            with ui.tabs().classes("w-full") as tabs:
                t_up, t_man, t_url, t_feed = ui.tab("Upload documents"), ui.tab("Enter manually"), ui.tab("From URL"), ui.tab("Feeds")
            with ui.tab_panels(tabs, value=t_up).classes("w-full"):
                with ui.tab_panel(t_up):
                    ui.label("We create the tender, store the original files, and extract fields and requirements automatically.").classes("tp-muted")
                    holder = ui.column().classes("w-full")

                    def create_for_upload():
                        def do(db):
                            t = tenders.create_tender(db, pc.ctx, {"title": "New tender (processing…)", "source_platform": "Manual upload"})
                            t.extraction = {}
                            return t.id
                        tid = run(do)
                        if tid:
                            holder.clear()
                            with holder:
                                upload_box(pc, tender=tid, role="main", on_done=lambda: ui.navigate.to(f"/tenders/{tid}"),
                                           label="Upload NIT / RFP, annexures, BOQ (XLSX) or a ZIP")
                                ui.link("Open tender", f"/tenders/{tid}").classes("tp-link")
                    ui.button("Start upload", icon="upload", on_click=create_for_upload).props("unelevated color=primary")
                with ui.tab_panel(t_man):
                    f = {}
                    with ui.grid(columns=2).classes("w-full gap-3"):
                        f["title"] = ui.input("Title *").props("outlined")
                        f["reference_number"] = ui.input("Reference number").props("outlined")
                        f["procuring_organization"] = ui.input("Procuring organization").props("outlined")
                        f["source_platform"] = ui.input("Source platform (e.g. CPPP, GeM)").props("outlined")
                        f["source_url"] = ui.input("Official source URL").props("outlined")
                        f["estimated_value"] = ui.number("Estimated value (₹)").props("outlined")
                        f["location_state"] = ui.input("State / location").props("outlined")
                        f["submission_deadline"] = ui.input("Submission deadline (IST)", placeholder="YYYY-MM-DD HH:MM").props("outlined")
                    f["description"] = ui.textarea("Description").props("outlined").classes("w-full")

                    def create_manual():
                        def do(db):
                            data = {k: (v.value or None) for k, v in f.items()}
                            if data.get("estimated_value") is not None:
                                data["estimated_value"] = Decimal(str(data["estimated_value"]))
                            if data.get("submission_deadline"):
                                try:
                                    data["submission_deadline"] = datetime.strptime(data["submission_deadline"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
                                except ValueError:
                                    from app.core.errors import ValidationFailed

                                    raise ValidationFailed("Deadline must be YYYY-MM-DD HH:MM (IST).")
                            return tenders.create_tender(db, pc.ctx, data).id
                        tid = run(do, success="Tender created")
                        if tid:
                            ui.navigate.to(f"/tenders/{tid}")
                    ui.button("Create tender", on_click=create_manual).props("unelevated color=primary")
                with ui.tab_panel(t_url):
                    ui.label("Only retrieve documents you are authorised to download. Portals requiring login or CAPTCHA are not accessed — "
                             "download the file yourself and upload it.").classes("tp-muted")
                    url = ui.input("Tender document URL").props("outlined").classes("w-full")

                    def do_url():
                        res = run(lambda db: tenders.import_from_url(db, pc.ctx, url.value or ""), success="Import started")
                        if res:
                            ui.navigate.to(f"/tenders/{res[0].id}")
                    ui.button("Import", on_click=do_url).props("unelevated color=primary")
                with ui.tab_panel(t_feed):
                    for key, src in SOURCES.items():
                        with ui.row().classes("w-full items-center justify-between q-py-xs"):
                            with ui.column().classes("gap-0 col"):
                                ui.label(src.name).classes("text-weight-medium")
                                ui.label(src.status_note).classes("text-caption tp-muted")
                            if src.supported:
                                async def feed_upload(e, key=key):
                                    data = await e.file.read()
                                    res = run(lambda db: tenders.import_feed(db, pc.ctx, key, data), success="Feed imported")
                                    if res is not None:
                                        ui.notify(f"{len(res)} tender(s) added", type="positive")
                                        table.refresh()
                                ui.upload(label="CSV", auto_upload=True, on_upload=feed_upload).props("accept=.csv flat dense").style("width:180px")
                            else:
                                ui.badge("Not supported", color="grey-6").props("outline")
        if request.query_params.get("new"):
            new_dialog.open()


@ui.page("/tenders/{tender_id}")
def tender_detail(request: Request, tender_id: str):
    tid = parse_uuid(tender_id)
    with shell(request, "/tenders") as pc:
        if not pc:
            return
        try:
            with session_scope() as db:
                t = tenders.get_tender(db, pc.ctx, tid)
                latest = elig.latest_assessment(db, pc.ctx, tid)
                ws = db.scalar(select(BidWorkspace).where(BidWorkspace.tender_id == tid, BidWorkspace.organization_id == pc.ctx.organization_id))
                ws_id = ws.id if ws else None
                info = {k: getattr(t, k) for k in ("title", "reference_number", "procuring_organization", "status", "estimated_value", "submission_deadline",
                                                    "clarification_deadline", "prebid_meeting_at", "bid_opening_at", "publication_date", "emd_amount", "emd_text",
                                                    "tender_fee", "performance_security_text", "location_state", "source_platform", "source_url",
                                                    "current_version_no", "extraction_method", "is_synthetic", "lock_version")}
                info["citations"] = (t.extraction or {}).get("citations", {})
                info["risk"] = (t.extraction or {}).get("risk_clauses", [])
                info["runs"] = (t.extraction or {}).get("runs", [])
                latest_info = (latest.id, latest.status, latest.overall_status, latest.readiness_score, latest.run_no) if latest else None
                next_states = sorted(lifecycle.allowed_next(t.status))
        except AppError as exc:
            ui.label(exc.message).classes("text-negative")
            return
        with ui.row().classes("w-full items-start justify-between no-wrap"):
            with ui.column().classes("gap-1 col"):
                ui.link("← All tenders", "/tenders").classes("tp-link text-caption")
                ui.label(info["title"]).classes("tp-h1")
                with ui.row().classes("items-center gap-2"):
                    ui.badge(info["status"].replace("_", " "), color=STATUS_COLOR.get(info["status"], "grey-7"))
                    ui.label(f"{info['reference_number'] or 'No reference'} · {info['procuring_organization'] or 'Procuring entity not stated'} · version {info['current_version_no']}").classes("tp-muted")
                    if info["is_synthetic"]:
                        ui.badge("synthetic sample", color="orange").props("outline")
            with ui.row().classes("gap-2"):
                if pc.can(Perm.ELIGIBILITY_RUN):
                    def assess():
                        res = run(lambda db: elig.start_assessment(db, pc.ctx, tid)[1].id, success="Eligibility assessment started")
                        if res:
                            with progress_area:
                                job_tracker([res], on_done=lambda: ui.navigate.to(f"/tenders/{tid}?tab=eligibility"))
                    ui.button("Run eligibility check", icon="fact_check", on_click=assess).props("unelevated color=secondary")
                if pc.can(Perm.BIDS_MANAGE):
                    if ws_id:
                        ui.button("Open bid workspace", icon="workspaces", on_click=lambda: ui.navigate.to(f"/bids/{ws_id}")).props("outline color=primary")
                    else:
                        def mk_ws():
                            from app.services.bids import service as bids

                            wid = run(lambda db: bids.create_workspace(db, pc.ctx, tid).id, success="Bid workspace created")
                            if wid:
                                ui.navigate.to(f"/bids/{wid}")
                        ui.button("Create bid workspace", icon="add_task", on_click=mk_ws).props("outline color=primary")
                if pc.can(Perm.TENDERS_WRITE) and next_states:
                    with ui.button(icon="more_vert").props("flat round"):
                        with ui.menu():
                            for s in next_states:
                                ui.menu_item(f"Move to: {s.replace('_', ' ')}", on_click=lambda s=s: run(
                                    lambda db: lifecycle.transition(db, pc.ctx, tenders.get_tender(db, pc.ctx, tid), s), success="Status updated",
                                    refresh=lambda: ui.navigate.reload()))
        progress_area = ui.column().classes("w-full")
        with session_scope() as db:
            from app.db.models import ProcessingJob

            active = [j.id for j in db.scalars(select(ProcessingJob).where(ProcessingJob.organization_id == pc.ctx.organization_id,
                                                                            ProcessingJob.status.in_(["queued", "running", "retrying"]))).all()]
        if active:
            with progress_area:
                ui.label("Processing in progress").classes("text-caption tp-muted")
                # Reload when processing finishes, keeping the tab the user is looking at.
                job_tracker(active, on_done=lambda: ui.navigate.to(f"/tenders/{tid}?tab={_tab_name(tabs.value)}"))
        tab_q = request.query_params.get("tab", "overview")
        with ui.tabs().classes("w-full") as tabs:
            tab_objs = {k: ui.tab(k, label=v) for k, v in [("overview", "Overview"), ("documents", "Documents"), ("requirements", "Requirements"),
                                                            ("eligibility", "Eligibility"), ("amendments", "Corrigenda"), ("activity", "Activity")]}
        with ui.tab_panels(tabs, value=tab_objs.get(tab_q, tab_objs["overview"])).classes("w-full bg-transparent"):
            with ui.tab_panel(tab_objs["overview"]):
                _overview(info, latest_info, tid)
            with ui.tab_panel(tab_objs["documents"]):
                _documents(pc, tid)
            with ui.tab_panel(tab_objs["requirements"]):
                _requirements(pc, tid)
            with ui.tab_panel(tab_objs["eligibility"]):
                _eligibility(pc, tid)
            with ui.tab_panel(tab_objs["amendments"]):
                _amendments(pc, tid)
            with ui.tab_panel(tab_objs["activity"]):
                with session_scope() as db:
                    hist = [(e.from_status, e.to_status, e.note, e.created_at) for e in tenders.status_history(db, pc.ctx, tid)]
                with ui.timeline(side="right").classes("w-full"):
                    for frm, to, note, at in reversed(hist):
                        ui.timeline_entry(note or "", title=f"{(frm or 'created').replace('_', ' ')} → {to.replace('_', ' ')}", subtitle=at.astimezone(IST).strftime("%d %b %Y %H:%M"))


def _cite(info, field):
    c = info["citations"].get(field)
    if c:
        ui.icon("format_quote", size="16px").classes("text-secondary").tooltip(f"p.{c.get('page')}: {c.get('text', '')[:200]}")


def _overview(info, latest_info, tid):
    with ui.row().classes("w-full gap-4 items-start"):
        with ui.card().classes("tp-card col q-pa-md"):
            ui.label("Key information").classes("tp-h2")
            rows = [("Estimated value", format_inr(info["estimated_value"]) if info["estimated_value"] else "Not stated", "estimated_value"),
                    ("Submission deadline", _dt(info["submission_deadline"]), "submission_deadline"),
                    ("Clarification deadline", _dt(info["clarification_deadline"]), "clarification_deadline"),
                    ("Pre-bid meeting", _dt(info["prebid_meeting_at"]), "prebid_meeting_at"),
                    ("Bid opening", _dt(info["bid_opening_at"]), "bid_opening_at"),
                    ("Published", info["publication_date"].strftime("%d %b %Y") if info["publication_date"] else "—", "publication_date"),
                    ("EMD", format_inr(info["emd_amount"]) if info["emd_amount"] else "—", "emd_amount"),
                    ("Tender fee", format_inr(info["tender_fee"]) if info["tender_fee"] else "—", "tender_fee"),
                    ("Location", info["location_state"] or "—", "location_state"),
                    ("Source", info["source_platform"] or "—", None)]
            for label, value, field in rows:
                with ui.row().classes("w-full justify-between q-py-xs no-wrap").style("border-bottom:1px solid #EEF1F5"):
                    ui.label(label).classes("tp-muted")
                    with ui.row().classes("items-center gap-1 no-wrap"):
                        ui.label(value).classes("text-weight-medium")
                        if field:
                            _cite(info, field)
            ui.label(f"Extraction: {info['extraction_method'] or 'pending'} · deadlines shown in IST, stored in UTC").classes("text-caption tp-muted q-mt-sm")
            for r in info["runs"]:
                if r.get("warning"):
                    ui.label(r["warning"]).classes("text-caption text-warning")
        with ui.column().classes("col gap-4"):
            with ui.card().classes("tp-card w-full q-pa-md"):
                ui.label("Eligibility").classes("tp-h2")
                if latest_info:
                    aid, status, overall, score, run_no = latest_info
                    with ui.row().classes("items-center gap-3"):
                        overall_badge(overall)
                        ui.label(f"Readiness {score:.0f}/100" if score is not None else "").classes("text-weight-bold")
                    ui.label(f"Run #{run_no} · {status.replace('_', ' ')}. Readiness is explanatory and never overrides mandatory conditions.").classes("text-caption tp-muted")
                    ui.link("Open assessment report →", f"/assessments/{aid}").classes("tp-link")
                else:
                    ui.label("Not assessed yet. Review the extracted requirements, then run the eligibility check.").classes("tp-muted")
            with ui.card().classes("tp-card w-full q-pa-md"):
                ui.label("Clauses that may need attention").classes("tp-h2")
                if not info["risk"]:
                    ui.label("No risk clauses detected by rules.").classes("tp-muted")
                for r in info["risk"][:8]:
                    ui.label(r).classes("tp-quote q-mb-xs")
                if info["emd_text"]:
                    ui.label("EMD clause").classes("text-caption tp-muted q-mt-sm")
                    ui.label(info["emd_text"]).classes("tp-quote")


def _documents(pc, tid):
    @ui.refreshable
    def body():
        with session_scope() as db:
            rows = [(td.role, d.id, d.title, v.id if v else None, v.processing_status if v else "—", v.page_count if v else None,
                     v.original_filename if v else "", v.quality_notes if v else [], v.content_type if v else "")
                    for td, d, v in tenders.tender_documents(db, pc.ctx, tid)]
        for role, did, title, vid, status, pages, fname, notes, ctype in rows:
            with ui.card().classes("tp-card w-full q-pa-sm"):
                with ui.row().classes("w-full items-center justify-between no-wrap"):
                    with ui.row().classes("items-center gap-2 no-wrap"):
                        ui.icon("table_view" if "sheet" in ctype else "picture_as_pdf" if "pdf" in ctype else "description").classes("text-secondary")
                        with ui.column().classes("gap-0"):
                            ui.label(fname).classes("text-weight-medium")
                            ui.label(f"{role} · {status} · {pages or '?'} page(s)").classes("text-caption tp-muted")
                    with ui.row():
                        if vid and "pdf" in ctype:
                            ui.button("View", icon="visibility", on_click=lambda vid=vid: ui.navigate.to(f"/viewer/{vid}")).props("flat dense")
                        if vid:
                            download_button(pc, vid)
                for n in notes:
                    ui.label(n).classes("text-caption text-warning")
        if not rows:
            ui.label("No documents attached yet.").classes("tp-muted")

    body()
    if pc.can(Perm.TENDERS_WRITE):
        with ui.row().classes("w-full gap-4"):
            with ui.column().classes("col"):
                ui.label("Add tender documents / annexures / BOQ").classes("tp-h2")
                upload_box(pc, tender=tid, role="main", on_done=body.refresh)
            with ui.column().classes("col"):
                ui.label("Upload a corrigendum / amendment").classes("tp-h2")
                ui.label("Changes are compared clause-by-clause; affected tasks and approvals are flagged.").classes("text-caption tp-muted")
                upload_box(pc, tender=tid, role="corrigendum", on_done=lambda: ui.navigate.to(f"/tenders/{tid}?tab=amendments"), label="Corrigendum (PDF/DOCX)")


MAND_COLOR = {"mandatory": "negative", "conditional": "orange", "optional": "grey-7", "unknown": "grey-6"}


def _requirements(pc, tid):
    @ui.refreshable
    def body():
        with session_scope() as db:
            reqs = [{"id": r.id, "code": r.code, "type": r.req_type, "title": r.title, "mandatory": r.mandatory, "page": r.source_page, "text": r.source_text,
                     "conf": r.extraction_confidence, "method": r.extraction_method, "review": r.review_status, "conditions": r.conditions, "notes": r.ambiguity_notes,
                     "threshold": r.threshold_value, "unit": r.threshold_unit, "period": r.period_years, "vid": r.source_document_version_id, "bbox": r.source_bbox,
                     "version": r.tender_version_no}
                    for r in tenders.requirements(db, pc.ctx, tid)]
        if not reqs:
            empty_state("checklist", "No requirements extracted yet", "Requirements appear here once the tender documents finish processing.")
            return
        ui.label(f"{len(reqs)} requirement(s). Extraction confidence is separate from eligibility status; confirm or correct each interpretation.").classes("tp-muted")
        for r in reqs:
            with ui.card().classes("tp-card w-full q-pa-md"):
                with ui.row().classes("w-full items-start justify-between no-wrap"):
                    with ui.column().classes("gap-1 col"):
                        with ui.row().classes("items-center gap-2"):
                            ui.label(r["code"]).classes("text-weight-bold")
                            ui.badge(r["type"].replace("_", " "), color="primary").props("outline")
                            ui.badge(r["mandatory"], color=MAND_COLOR.get(r["mandatory"], "grey"))
                            ui.badge(f"review: {r['review']}", color="positive" if r["review"] in ("confirmed", "edited") else "grey-6").props("outline")
                            if r["version"] > 1:
                                ui.badge(f"from corrigendum v{r['version']}", color="orange").props("outline")
                        ui.label(r["title"]).classes("text-subtitle2")
                        if r["threshold"] is not None:
                            ui.label(f"Threshold: {format_inr(r['threshold']) if r['unit'] == 'INR' else r['threshold']} {'' if r['unit'] == 'INR' else (r['unit'] or '')}"
                                     + (f" · period: {r['period']} years" if r["period"] else "")).classes("text-caption")
                        with ui.row().classes("items-center gap-1"):
                            ui.label(f"Tender p.{r['page']} · extraction confidence {r['conf']:.0%} ({r['method']})").classes("text-caption tp-muted")
                            if r["vid"]:
                                ui.link("open source", f"/viewer/{r['vid']}?page={r['page']}&req={r['id']}").classes("tp-link text-caption")
                        ui.label(r["text"]).classes("tp-quote")
                        for c in r["conditions"]:
                            ui.label(f"Condition: {c}").classes("tp-quote").style("border-left-color:#E67E22;background:#FDF5EC")
                        if r["notes"]:
                            ui.label(f"⚠ {r['notes']}").classes("text-caption text-warning")
                    if pc.can(Perm.ELIGIBILITY_REVIEW):
                        with ui.column().classes("gap-1"):
                            ui.button("Confirm", icon="check", on_click=lambda rid=r["id"]: run(lambda db: tenders.review_requirement(db, pc.ctx, rid, "confirm"),
                                                                                                success="Confirmed", refresh=body.refresh)).props("dense unelevated color=positive")
                            ui.button("Edit", icon="edit", on_click=lambda r=r: edit(r)).props("dense flat")
                            ui.button("Reject", icon="close", on_click=lambda rid=r["id"]: run(lambda db: tenders.review_requirement(db, pc.ctx, rid, "reject", note="Not a requirement"),
                                                                                              success="Rejected", refresh=body.refresh)).props("dense flat color=negative")

    def edit(r):
        with ui.dialog() as dlg, ui.card().style("width:560px"):
            ui.label(f"Edit interpretation of {r['code']}").classes("tp-h2")
            ui.label("The original clause is kept; only the structured interpretation changes.").classes("text-caption tp-muted")
            title = ui.input("Title", value=r["title"]).props("outlined").classes("w-full")
            thr = ui.input("Threshold (base units, e.g. rupees)", value=str(r["threshold"]) if r["threshold"] is not None else "").props("outlined").classes("w-full")
            period = ui.number("Period (years)", value=r["period"]).props("outlined").classes("w-full")
            mand = ui.select(["mandatory", "conditional", "optional", "unknown"], value=r["mandatory"], label="Mandatory").props("outlined").classes("w-full")

            def save():
                changes = {"title": title.value, "mandatory": mand.value, "period_years": int(period.value) if period.value else None}
                if thr.value.strip():
                    changes["threshold_value"] = thr.value.strip()
                if run(lambda db: tenders.review_requirement(db, pc.ctx, r["id"], "edit", changes), success="Saved", refresh=body.refresh):
                    dlg.close()
            with ui.row():
                ui.button("Save", on_click=save).props("unelevated color=primary")
                ui.button("Cancel", on_click=dlg.close).props("flat")
        dlg.open()

    body()


def _eligibility(pc, tid):
    with session_scope() as db:
        runs = [(a.id, a.run_no, a.status, a.overall_status, a.readiness_score, a.trigger, a.created_at) for a in elig.list_assessments(db, pc.ctx, tid)]
    if not runs:
        empty_state("fact_check", "No assessments yet", "Run the eligibility check to compare every requirement with your evidence.")
        return
    for aid, run_no, status, overall, score, trigger, at in runs:
        with ui.card().classes("tp-card w-full q-pa-md"):
            with ui.row().classes("w-full items-center justify-between"):
                with ui.row().classes("items-center gap-3"):
                    ui.label(f"Run #{run_no}").classes("text-weight-bold")
                    overall_badge(overall)
                    ui.label(f"{status.replace('_', ' ')} · trigger: {trigger} · {at.astimezone(IST):%d %b %Y %H:%M}").classes("tp-muted")
                with ui.row().classes("items-center gap-2"):
                    ui.label(f"{score:.0f}/100" if score is not None else "").classes("text-weight-bold")
                    ui.button("Report", on_click=lambda aid=aid: ui.navigate.to(f"/assessments/{aid}")).props("dense unelevated color=primary")
                    ui.button("Graph", on_click=lambda aid=aid: ui.navigate.to(f"/evidence/{aid}")).props("dense flat")
    if len(runs) > 1:
        with session_scope() as db:
            diff = elig.compare_assessments(db, pc.ctx, runs[1][0], runs[0][0])
        with ui.card().classes("tp-card w-full q-pa-md"):
            ui.label(f"What changed between run #{runs[1][1]} and run #{runs[0][1]}").classes("tp-h2")
            for d in diff:
                ui.label(f"{d['code']}: {(d['before'] or '—').replace('_', ' ')} → {(d['after'] or '—').replace('_', ' ')}").classes("text-body2")
            if not diff:
                ui.label("No outcome changes.").classes("tp-muted")


def _amendments(pc, tid):
    with session_scope() as db:
        rows = [(a.number, a.status, a.summary, a.field_changes, a.requirement_changes, a.impact, a.from_version_no, a.to_version_no, a.created_at)
                for a in tenders.amendments(db, pc.ctx, tid)]
    if not rows:
        empty_state("published_with_changes", "No corrigenda", "Upload a corrigendum from the Documents tab; TenderPilot shows exactly what changed.")
        return
    for num, status, summary, fchanges, rchanges, impact, v_from, v_to, at in rows:
        with ui.card().classes("tp-card w-full q-pa-md"):
            ui.label(f"Corrigendum {num or ''} · v{v_from} → v{v_to or '?'} · {status}").classes("tp-h2")
            ui.label(summary or "Processing…").classes("tp-muted")
            for c in fchanges:
                ui.label(f"Field {c['field'].replace('_', ' ')}: {c['from']} → {c['to']}").classes("text-body2")
            for c in rchanges:
                with ui.card().classes("w-full q-pa-sm q-my-xs").props("flat bordered"):
                    ui.label(f"{c['change'].upper()} {c['code']} — {c['title']}").classes("text-weight-medium")
                    if c["before"]["text"]:
                        ui.label(c["before"]["text"]).classes("tp-quote").style("border-left-color:#C0392B;background:#FDF0EE;text-decoration:line-through")
                    ui.label(c["after"]["text"]).classes("tp-quote")
            if impact:
                ui.label("Impact: " + ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in impact.items())).classes("text-caption tp-muted")


_ = uuid

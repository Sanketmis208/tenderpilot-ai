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
from app.ui.themes.theme import empty_state, overall_badge, page_header, panel, score_ring


def _tab_name(value) -> str:
    return value if isinstance(value, str) else getattr(value, "props", {}).get("name", "overview")


STATUS_COLOR = {"discovered": "grey-7", "imported": "grey-7", "ready_for_review": "info", "processing": "warning", "under_evaluation": "info",
                "shortlisted": "accent", "bid_preparation": "primary", "won": "positive", "lost": "negative", "awaiting_approval": "orange",
                "ready_for_external_submission": "positive", "submitted_externally": "accent"}

_JS_STATUS_COLOR = "{" + ",".join(f"{k}:'{v}'" for k, v in STATUS_COLOR.items()) + "}"


def _dt(v: datetime | None) -> str:
    return v.astimezone(IST).strftime("%d %b %Y, %H:%M IST") if v else "—"


@ui.page("/tenders")
def tender_list(request: Request):
    with shell(request, "/tenders") as pc:
        if not pc:
            return
        state = {"q": "", "status": None, "sort": "deadline", "page": 0}
        with page_header("My Tenders", "Every tender you've imported — with deadlines, value and where it is in your pipeline."):
            if pc.can(Perm.TENDERS_WRITE):
                ui.button("Add tender", icon="add", on_click=lambda: new_dialog.open()).props("unelevated color=primary")
        with ui.row().classes("w-full gap-2 items-center"):
            q = ui.input(placeholder="Search title, reference, organization").props("outlined dense clearable").classes("col").style("min-width:240px")
            with q.add_slot("prepend"):
                ui.icon("search", size="18px").style("color:#98A2B3")
            st = ui.select({None: "All statuses", **{s.value: s.value.replace("_", " ").capitalize() for s in TenderStatus}}, value=None).props("outlined dense").style("width:220px")
            so = ui.select({"deadline": "Sort: Deadline", "value": "Sort: Value", "newest": "Sort: Newest", "title": "Sort: Title"}, value="deadline").props("outlined dense").style("width:170px")

        @ui.refreshable
        def table():
            with session_scope() as db:
                rows, total = tenders.list_tenders(db, pc.ctx, q=state["q"] or None, status=[state["status"]] if state["status"] else None,
                                                   sort=state["sort"], offset=state["page"] * 20, limit=20)
                data = [{"id": str(t.id), "title": t.title, "ref": t.reference_number or "—", "org": t.procuring_organization or "—", "status": t.status,
                         "value": format_inr_short(t.estimated_value) if t.estimated_value else "—", "deadline": _dt(t.submission_deadline)} for t in rows]
            if not data and not state["q"] and not state["status"]:
                with panel():
                    empty_state("description", "No tenders yet", "Upload a tender document (PDF/DOCX/ZIP) or enter one manually.", "Add tender", lambda: new_dialog.open())
                return
            cols = [{"name": "title", "label": "Tender", "field": "title", "align": "left"}, {"name": "ref", "label": "Reference", "field": "ref", "align": "left"},
                    {"name": "org", "label": "Procuring entity", "field": "org", "align": "left"}, {"name": "value", "label": "Est. value", "field": "value"},
                    {"name": "deadline", "label": "Submission", "field": "deadline"}, {"name": "status", "label": "Status", "field": "status"}]
            cols = [c for c in cols if c["name"] != "ref"]
            widths = {"title": "min-width:320px;width:38%", "org": "min-width:180px;width:24%", "value": "width:110px", "deadline": "width:170px", "status": "width:150px"}
            for c in cols:
                c["align"] = "right" if c["name"] == "value" else "left"
                c["headerStyle"] = widths.get(c["name"], "")
                c["style"] = widths.get(c["name"], "") + ";white-space:normal"
            t = ui.table(columns=cols, rows=data, row_key="id", pagination=0).props("flat hide-bottom").classes("w-full tp-card")
            t.add_slot("body-cell-title", '<q-td :props="props" style="max-width:460px;white-space:normal"><a class="tp-link" style="color:#101828" :href="\'/tenders/\' + props.row.id">{{ props.value }}</a>'
                                          '<div class="tp-caption">{{ props.row.ref }}</div></q-td>')
            t.add_slot("body-cell-status", '<q-td :props="props"><q-badge class="tp-chip" :color="(' + _JS_STATUS_COLOR + ')[props.value] || \'grey-7\'" '
                                           ':label="props.value.charAt(0).toUpperCase() + props.value.slice(1).replaceAll(\'_\',\' \')" /></q-td>')
            if not data:
                ui.label("No tenders match these filters.").classes("tp-muted q-pa-md")
            with ui.row().classes("w-full justify-between items-center"):
                start = state["page"] * 20
                ui.label(f"Showing {start + 1 if total else 0}–{min(start + 20, total)} of {total} tender(s)").classes("tp-caption")
                with ui.row().classes("gap-2"):
                    ui.button("Previous", icon="chevron_left", on_click=lambda: (state.update(page=max(0, state["page"] - 1)), table.refresh())).props("outline dense color=primary").set_enabled(state["page"] > 0)
                    ui.button("Next", on_click=lambda: (state.update(page=state["page"] + 1), table.refresh())).props("outline dense color=primary icon-right=chevron_right").set_enabled((state["page"] + 1) * 20 < total)

        q.on("update:model-value", lambda e: (state.update(q=q.value, page=0), table.refresh()), throttle=0.4)
        st.on_value_change(lambda e: (state.update(status=e.value, page=0), table.refresh()))
        so.on_value_change(lambda e: (state.update(sort=e.value), table.refresh()))
        table()

        with ui.dialog() as new_dialog, ui.card().style("width: 720px; max-width: 96vw"):
            with ui.row().classes("w-full items-start justify-between no-wrap"):
                with ui.column().classes("gap-0"):
                    ui.label("Add a tender").classes("tp-h1").style("font-size:1.25rem")
                    ui.label("Upload the documents, enter details by hand, fetch from a URL or import a feed.").classes("tp-caption")
                ui.button(icon="close", on_click=lambda: new_dialog.close()).props("flat round dense").classes("tp-icon-btn")
            with ui.tabs().classes("w-full").props("align=left") as tabs:
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
                    ui.button("Start upload", icon="upload", on_click=create_for_upload).props("unelevated color=primary").classes("self-start")
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
                    ui.button("Create tender", on_click=create_manual).props("unelevated color=primary").classes("self-end")
                with ui.tab_panel(t_url):
                    ui.label("Only retrieve documents you are authorised to download. Portals requiring login or CAPTCHA are not accessed — "
                             "download the file yourself and upload it.").classes("tp-muted")
                    url = ui.input("Tender document URL").props("outlined").classes("w-full")

                    def do_url():
                        res = run(lambda db: tenders.import_from_url(db, pc.ctx, url.value or ""), success="Import started")
                        if res:
                            ui.navigate.to(f"/tenders/{res[0].id}")
                    ui.button("Import", icon="link", on_click=do_url).props("unelevated color=primary").classes("self-start")
                with ui.tab_panel(t_feed):
                    for key, src in SOURCES.items():
                        with ui.row().classes("w-full items-center justify-between tp-row-line no-wrap gap-3"):
                            with ui.column().classes("gap-0 col"):
                                ui.label(src.name).classes("text-weight-medium").style("color:#101828")
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
                                ui.badge("Not supported", color="grey-6").classes("tp-chip")
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
        def _meta():
            ui.badge(info["status"].replace("_", " ").capitalize(), color=STATUS_COLOR.get(info["status"], "grey-7")).classes("tp-chip")
            for icon, text in (("tag", info["reference_number"] or "No reference"), ("account_balance", info["procuring_organization"] or "Procuring entity not stated"),
                               ("history", f"Version {info['current_version_no']}")):
                with ui.row().classes("items-center gap-1 no-wrap tp-caption"):
                    ui.icon(icon, size="15px")
                    ui.label(text)
            if info["is_synthetic"]:
                ui.badge("Synthetic sample", color="orange").props("outline").classes("tp-chip")

        with page_header(info["title"], back=("All tenders", "/tenders"), meta=_meta):
                if pc.can(Perm.ELIGIBILITY_RUN):
                    def assess():
                        res = run(lambda db: elig.start_assessment(db, pc.ctx, tid)[1].id, success="Eligibility assessment started")
                        if res:
                            with progress_area:
                                job_tracker([res], on_done=lambda: ui.navigate.to(f"/tenders/{tid}?tab=eligibility"))
                    ui.button("Run eligibility check", icon="fact_check", on_click=assess).props("unelevated color=primary")
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
                    with ui.button(icon="more_horiz").props("outline color=primary").tooltip("Change status"):
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
            with progress_area, ui.card().classes("tp-card tp-panel w-full").props("flat").style("background:#F8FAFF;border-color:#D1E0FF"):
                with ui.row().classes("items-center gap-2"):
                    ui.spinner(size="18px", color="primary")
                    ui.label("Processing in progress").classes("tp-h3")
                # Reload when processing finishes, keeping the tab the user is looking at.
                job_tracker(active, on_done=lambda: ui.navigate.to(f"/tenders/{tid}?tab={_tab_name(tabs.value)}"))
        tab_q = request.query_params.get("tab", "overview")
        with ui.tabs().classes("w-full").props("align=left") as tabs:
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
                with panel("Status history", icon="timeline"), ui.timeline(side="right", color="primary").classes("w-full"):
                    for frm, to, note, at in reversed(hist):
                        ui.timeline_entry(note or "", title=f"{(frm or 'created').replace('_', ' ')} → {to.replace('_', ' ')}", subtitle=at.astimezone(IST).strftime("%d %b %Y %H:%M"))


def _cite(info, field):
    c = info["citations"].get(field)
    if c:
        ui.icon("format_quote", size="16px").classes("text-secondary").tooltip(f"p.{c.get('page')}: {c.get('text', '')[:200]}")


def _overview(info, latest_info, tid):
    with ui.element("div").classes("tp-grid-2 w-full"):
        with panel("Key information", "Extracted from the tender documents — hover the quote icon for the source", icon="info"):
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
            with ui.column().classes("w-full gap-0"):
                for label, value, field in rows:
                    with ui.row().classes("w-full justify-between no-wrap tp-row-line gap-4"):
                        ui.label(label).classes("tp-muted")
                        with ui.row().classes("items-center gap-1 no-wrap"):
                            ui.label(value).classes("text-weight-medium text-right tp-num").style("color:#101828")
                            if field:
                                _cite(info, field)
            ui.label(f"Extraction: {info['extraction_method'] or 'pending'} · deadlines shown in IST, stored in UTC").classes("text-caption tp-muted q-mt-sm")
            for r in info["runs"]:
                if r.get("warning"):
                    ui.label(r["warning"]).classes("text-caption text-warning")
        with ui.column().classes("w-full gap-4"):
            with panel("Eligibility", icon="fact_check"):
                if latest_info:
                    aid, status, overall, score, run_no = latest_info
                    with ui.row().classes("items-center gap-4 no-wrap"):
                        score_ring(score)
                        with ui.column().classes("gap-1"):
                            overall_badge(overall)
                            ui.label(f"Readiness {score:.0f}/100 · run #{run_no} · {status.replace('_', ' ')}" if score is not None else f"Run #{run_no}").classes("tp-caption")
                    ui.label("Readiness is explanatory and never overrides mandatory conditions.").classes("tp-caption")
                    ui.button("Open assessment report", icon="arrow_forward", on_click=lambda: ui.navigate.to(f"/assessments/{aid}")).props("outline color=primary").classes("self-start")
                else:
                    ui.label("Not assessed yet. Review the extracted requirements, then run the eligibility check.").classes("tp-muted")
            with panel("Clauses that may need attention", icon="warning_amber"):
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
            with ui.card().classes("tp-card w-full").props("flat").style("padding:12px 16px"):
                with ui.row().classes("w-full items-center justify-between no-wrap"):
                    with ui.row().classes("items-center gap-3 no-wrap"):
                        sheet, pdf = "sheet" in ctype, "pdf" in ctype
                        ui.icon("table_view" if sheet else "picture_as_pdf" if pdf else "description").classes(
                            "tp-feature-icon " + ("tp-tone-green" if sheet else "tp-tone-rose" if pdf else "tp-tone-primary")).style("width:38px;height:38px;font-size:20px")
                        with ui.column().classes("gap-0"):
                            ui.label(fname).classes("text-weight-medium").style("color:#101828")
                            with ui.row().classes("items-center gap-2"):
                                ui.badge(role.replace("_", " ").capitalize(), color="grey-7").classes("tp-chip")
                                ui.label(f"{status} · {pages or '?'} page(s)").classes("tp-caption")
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
        with ui.element("div").classes("tp-grid-2 w-full"):
            with panel("Add tender documents", "Annexures, BOQ workbooks or a ZIP of everything", icon="upload_file"):
                upload_box(pc, tender=tid, role="main", on_done=body.refresh)
            with panel("Upload a corrigendum", "Changes are compared clause-by-clause; affected tasks and approvals are flagged.", icon="published_with_changes"):
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
        with ui.row().classes("items-center gap-2"):
            ui.badge(f"{len(reqs)} requirement(s)", color="primary").classes("tp-chip")
            ui.label("Extraction confidence is separate from eligibility status; confirm or correct each interpretation.").classes("tp-caption")
        for r in reqs:
            with ui.card().classes("tp-card tp-panel w-full").props("flat"):
                with ui.row().classes("w-full items-start justify-between no-wrap"):
                    with ui.column().classes("gap-1 col"):
                        with ui.row().classes("items-center gap-2"):
                            ui.label(r["code"]).classes("tp-num").style("font-weight:650;color:#475467;font-size:.8rem;background:#F2F4F7;border-radius:6px;padding:1px 7px")
                            ui.badge(r["mandatory"].capitalize(), color=MAND_COLOR.get(r["mandatory"], "grey")).classes("tp-chip")
                            ui.badge(r["type"].replace("_", " ").capitalize(), color="grey-7").props("outline").classes("tp-chip")
                            ui.badge(f"Review: {r['review']}", color="positive" if r["review"] in ("confirmed", "edited") else "grey-6").props("outline").classes("tp-chip")
                            if r["version"] > 1:
                                ui.badge(f"From corrigendum v{r['version']}", color="orange").props("outline").classes("tp-chip")
                        ui.label(r["title"]).classes("text-subtitle1").style("color:#101828")
                        if r["threshold"] is not None:
                            ui.label(f"Threshold: {format_inr(r['threshold']) if r['unit'] == 'INR' else r['threshold']} {'' if r['unit'] == 'INR' else (r['unit'] or '')}"
                                     + (f" · period: {r['period']} years" if r["period"] else "")).classes("text-caption")
                        with ui.row().classes("items-center gap-1"):
                            ui.label(f"Tender p.{r['page']} · extraction confidence {r['conf']:.0%} ({r['method']})").classes("text-caption tp-muted")
                            if r["vid"]:
                                ui.link("open source", f"/viewer/{r['vid']}?page={r['page']}&req={r['id']}").classes("tp-link text-caption")
                        ui.label(r["text"]).classes("tp-quote")
                        for c in r["conditions"]:
                            ui.label(f"Condition: {c}").classes("tp-quote").style("border-left-color:#F79009;background:#FFFAEB")
                        if r["notes"]:
                            ui.label(f"⚠ {r['notes']}").classes("text-caption text-warning")
                    if pc.can(Perm.ELIGIBILITY_REVIEW):
                        with ui.column().classes("gap-1").style("min-width:110px"):
                            ui.button("Confirm", icon="check", on_click=lambda rid=r["id"]: run(lambda db: tenders.review_requirement(db, pc.ctx, rid, "confirm"),
                                                                                                success="Confirmed", refresh=body.refresh)).props("dense unelevated color=positive")
                            ui.button("Edit", icon="edit", on_click=lambda r=r: edit(r)).props("dense flat")
                            ui.button("Reject", icon="close", on_click=lambda rid=r["id"]: run(lambda db: tenders.review_requirement(db, pc.ctx, rid, "reject", note="Not a requirement"),
                                                                                              success="Rejected", refresh=body.refresh)).props("dense flat color=negative")

    def edit(r):
        with ui.dialog() as dlg, ui.card().style("width:560px"):
            ui.label(f"Edit interpretation of {r['code']}").classes("tp-h1").style("font-size:1.2rem")
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
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Cancel", on_click=dlg.close).props("outline color=primary")
                ui.button("Save", on_click=save).props("unelevated color=primary")
        dlg.open()

    body()


def _eligibility(pc, tid):
    with session_scope() as db:
        runs = [(a.id, a.run_no, a.status, a.overall_status, a.readiness_score, a.trigger, a.created_at) for a in elig.list_assessments(db, pc.ctx, tid)]
    if not runs:
        empty_state("fact_check", "No assessments yet", "Run the eligibility check to compare every requirement with your evidence.")
        return
    for aid, run_no, status, overall, score, trigger, at in runs:
        with ui.card().classes("tp-card w-full").props("flat").style("padding:14px 18px"):
            with ui.row().classes("w-full items-center justify-between no-wrap gap-3"):
                with ui.row().classes("items-center gap-4 no-wrap"):
                    score_ring(score)
                    with ui.column().classes("gap-1"):
                        with ui.row().classes("items-center gap-2"):
                            ui.label(f"Run #{run_no}").classes("tp-h2")
                            overall_badge(overall)
                        ui.label(f"{status.replace('_', ' ').capitalize()} · trigger: {trigger} · {at.astimezone(IST):%d %b %Y %H:%M}").classes("tp-caption")
                with ui.row().classes("items-center gap-2 no-wrap"):
                    ui.button("Graph", icon="hub", on_click=lambda aid=aid: ui.navigate.to(f"/evidence/{aid}")).props("dense outline color=primary")
                    ui.button("Report", on_click=lambda aid=aid: ui.navigate.to(f"/assessments/{aid}")).props("dense unelevated color=primary")
    if len(runs) > 1:
        with session_scope() as db:
            diff = elig.compare_assessments(db, pc.ctx, runs[1][0], runs[0][0])
        with panel(f"What changed between run #{runs[1][1]} and run #{runs[0][1]}", icon="compare_arrows"):
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
        with panel(f"Corrigendum {num or ''}".strip(), f"Version {v_from} → {v_to or '?'} · {status} · {at.astimezone(IST):%d %b %Y}", icon="published_with_changes"):
            ui.label(summary or "Processing…").classes("text-body2")
            for c in fchanges:
                ui.label(f"Field {c['field'].replace('_', ' ')}: {c['from']} → {c['to']}").classes("text-body2")
            for c in rchanges:
                with ui.card().classes("w-full q-pa-sm q-my-xs").props("flat bordered"):
                    with ui.row().classes("items-center gap-2"):
                        ui.badge(c["change"].capitalize(), color={"added": "positive", "removed": "negative"}.get(c["change"], "warning")).classes("tp-chip")
                        ui.label(f"{c['code']} — {c['title']}").classes("text-weight-medium").style("color:#101828")
                    if c["before"]["text"]:
                        ui.label(c["before"]["text"]).classes("tp-quote").style("border-left-color:#F04438;background:#FEF3F2;text-decoration:line-through;color:#912018")
                    ui.label(c["after"]["text"]).classes("tp-quote").style("border-left-color:#12B76A;background:#ECFDF3")
            if impact:
                ui.label("Impact: " + ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in impact.items())).classes("text-caption tp-muted")


_ = uuid

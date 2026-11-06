"""Bid workspaces, team administration and settings."""

from __future__ import annotations

import html
from datetime import date
from decimal import Decimal

from fastapi import Request
from nicegui import ui
from sqlalchemy import select

from app.ai.workflows import bid_preparation
from app.auth.deps import parse_uuid
from app.auth.permissions import ROLE_LABELS, Perm
from app.core.config import get_settings
from app.core.errors import AppError
from app.db.models import Document, Organization, Role, Tender, User
from app.db.session import session_scope
from app.extraction.normalization.values import IST, format_inr, format_inr_short
from app.integrations.connectors import CONNECTORS
from app.services.bids import generation
from app.services.bids import service as bids
from app.services.billing import entitlements
from app.services.boq import service as boq
from app.services.notifications import service as notifications
from app.services.organizations import auth_service, org_service
from app.ui.components.common import download_button
from app.ui.layouts.shell import run, shell
from app.ui.themes.theme import empty_state, initials, page_header, panel

BID_STATUS_COLOR = {"draft": "grey-7", "internal_review": "info", "changes_requested": "warning", "approved": "positive", "ready_for_external_submission": "secondary"}
NEXT_ACTIONS = {"draft": ["submit_for_review"], "changes_requested": ["submit_for_review"], "internal_review": ["approve", "request_changes"],
                "approved": ["mark_ready", "reopen"], "ready_for_external_submission": ["reopen"]}


@ui.page("/bids")
def bids_list(request: Request):
    with shell(request, "/bids") as pc:
        if not pc:
            return
        with page_header("Bid Workspaces", "Collaborative rooms for the tenders you've decided to pursue — tasks, drafts, BOQ, reviews and exports."):
            ui.button("New from a tender", icon="add", on_click=lambda: ui.navigate.to("/tenders")).props("outline color=primary")
        with session_scope() as db:
            rows = [(w.id, w.name, w.status, t.title, t.submission_deadline, t.estimated_value, c) for w, t, c in bids.list_workspaces(db, pc.ctx)]
        if not rows:
            with ui.card().classes("tp-card w-full"):
                empty_state("workspaces", "No bid workspaces", "Create a workspace from a tender you decide to pursue.", "Go to tenders", lambda: ui.navigate.to("/tenders"))
            return
        with ui.element("div").classes("tp-grid-3 w-full"):
            for wid, name, status, title, dl, val, c in rows:
                total = (c["open"] or 0) + (c["done"] or 0)
                with ui.card().classes("tp-card tp-panel cursor-pointer").props("flat").on("click", lambda wid=wid: ui.navigate.to(f"/bids/{wid}")):
                    with ui.row().classes("w-full items-center justify-between no-wrap"):
                        ui.icon("workspaces").classes("tp-feature-icon tp-tone-violet").style("width:38px;height:38px;font-size:20px")
                        ui.badge(status.replace("_", " ").capitalize(), color=BID_STATUS_COLOR.get(status, "grey")).classes("tp-chip")
                    ui.label(title).classes("text-subtitle1").style("color:#101828;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden")
                    with ui.row().classes("items-center gap-3 tp-caption"):
                        with ui.row().classes("items-center gap-1"):
                            ui.icon("payments", size="15px")
                            ui.label(format_inr_short(val) if val else "—")
                        if dl:
                            with ui.row().classes("items-center gap-1"):
                                ui.icon("event", size="15px")
                                ui.label(f"{dl.astimezone(IST):%d %b %Y}")
                    with ui.column().classes("w-full gap-1"):
                        with ui.row().classes("w-full justify-between"):
                            ui.label("Tasks").classes("tp-caption")
                            ui.label(f"{c['done']}/{total} done").classes("tp-caption tp-num")
                        ui.linear_progress(value=(c["done"] / total) if total else 0, show_value=False)


@ui.page("/bids/{workspace_id}")
def bid_workspace(request: Request, workspace_id: str):
    wid = parse_uuid(workspace_id)
    with shell(request, "/bids") as pc:
        if not pc:
            return
        try:
            with session_scope() as db:
                ws = bids.get_workspace(db, pc.ctx, wid)
                t = db.get(Tender, ws.tender_id)
                head = (ws.name, ws.status, t.id, t.title, t.submission_deadline, t.reference_number)
                members = {str(u.id): u.full_name for m, u in org_service.list_members(db, pc.ctx) if m.is_active}
        except AppError as exc:
            ui.label(exc.message).classes("text-negative")
            return
        name, status, tid, ttitle, dl, ref = head

        def _meta():
            ui.badge(status.replace("_", " ").capitalize(), color=BID_STATUS_COLOR.get(status, "grey")).classes("tp-chip")
            if ref:
                with ui.row().classes("items-center gap-1 tp-caption no-wrap"):
                    ui.icon("tag", size="15px")
                    ui.label(ref)
            if dl:
                with ui.row().classes("items-center gap-1 tp-caption no-wrap"):
                    ui.icon("event", size="15px")
                    ui.label(f"Submission {dl.astimezone(IST):%d %b %Y %H:%M} IST")

        with page_header(name, back=(ttitle, f"/tenders/{tid}"), meta=_meta):
                ui.button("Ask copilot", icon="auto_awesome", on_click=lambda: ui.navigate.to(f"/copilot?tender={tid}")).props("outline color=primary")
                for action in NEXT_ACTIONS.get(status, []):
                    needed = {"submit_for_review": Perm.BIDS_WRITE, "approve": Perm.BIDS_APPROVE, "request_changes": Perm.BIDS_APPROVE, "mark_ready": Perm.BIDS_APPROVE, "reopen": Perm.BIDS_MANAGE}[action]
                    if pc.can(needed):
                        ui.button(action.replace("_", " ").capitalize(), on_click=lambda action=action: approval_dialog(action)).props(
                            "unelevated color=primary" if action in ("approve", "submit_for_review", "mark_ready") else "outline color=primary")
        _stepper(status)
        if status == "ready_for_external_submission":
            with ui.row().classes("w-full items-center no-wrap gap-3 tp-card").style("padding:14px 18px;background:#ECFDF3;border-color:#ABEFC6"):
                ui.icon("verified", size="22px").style("color:#079455")
                ui.label("This bid is approved and packaged. Submit it yourself through the official procurement portal; TenderPilot does not submit bids or apply digital signatures.").classes("text-body2").style("color:#085D3A")

        def approval_dialog(action):
            with ui.dialog() as dlg, ui.card().style("width:480px"):
                ui.label(action.replace("_", " ").capitalize()).classes("tp-h1").style("font-size:1.2rem")
                ui.label("Your decision and note are recorded in the bid's audit trail.").classes("tp-caption")
                note = ui.textarea("Note" + (" (required)" if action == "request_changes" else "")).props("outlined").classes("w-full")

                def go():
                    if run(lambda db: bids.approval_action(db, pc.ctx, wid, action, note.value), success="Recorded"):
                        dlg.close()
                        ui.navigate.reload()
                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button("Cancel", on_click=dlg.close).props("outline color=primary")
                    ui.button("Confirm", on_click=go).props("unelevated color=primary")
            dlg.open()

        tab_q = request.query_params.get("tab", "tasks")
        with ui.tabs().classes("w-full").props("align=left") as tabs:
            tb = {k: ui.tab(k, label=v) for k, v in [("tasks", "Tasks"), ("drafts", "Drafts"), ("boq", "BOQ"), ("review", "Review & approvals"),
                                                     ("documents", "Documents & exports"), ("activity", "Activity")]}
        with ui.tab_panels(tabs, value=tb.get(tab_q, tb["tasks"])).classes("w-full bg-transparent"):
            with ui.tab_panel(tb["tasks"]):
                _tasks(pc, wid, members)
            with ui.tab_panel(tb["drafts"]):
                _drafts(pc, wid, status)
            with ui.tab_panel(tb["boq"]):
                _boq(pc, wid, tid)
            with ui.tab_panel(tb["review"]):
                with session_scope() as db:
                    rows = [(a.action, a.from_status, a.to_status, a.draft_version_no, members.get(str(a.actor_id), "—"), a.note, a.created_at) for a in bids.approvals(db, pc.ctx, wid)]
                with panel("Review & approvals", "Draft → Internal review → Changes requested → Approved → Ready for external submission. Approvers cannot approve their own drafts.", icon="verified_user"):
                    for action, frm, to, dv, who, note, at in rows:
                        with ui.row().classes("w-full items-start no-wrap gap-3 tp-row-line"):
                            ui.html(f'<div class="tp-avatar sm">{html.escape(initials(who))}</div>')
                            with ui.column().classes("gap-0 col"):
                                with ui.row().classes("items-center gap-2"):
                                    ui.label(who).classes("text-weight-medium").style("color:#101828")
                                    ui.badge(action.replace("_", " ").capitalize(), color={"approve": "positive", "request_changes": "warning"}.get(action, "grey-7")).classes("tp-chip")
                                    ui.label(f"{frm} → {to}" + (f" · draft v{dv}" if dv else "")).classes("tp-caption")
                                if note:
                                    ui.label(note).classes("text-body2")
                            ui.label(f"{at.astimezone(IST):%d %b %H:%M}").classes("tp-caption")
                    if not rows:
                        ui.label("No approval activity yet.").classes("tp-muted")
            with ui.tab_panel(tb["documents"]):
                _exports(pc, wid)
            with ui.tab_panel(tb["activity"]):
                with session_scope() as db:
                    ev = [(e.action, members.get(str(e.actor_id), "system"), e.created_at) for e in bids.timeline(db, pc.ctx, wid)]
                with panel("Activity", icon="timeline"), ui.timeline(side="right", color="primary").classes("w-full"):
                    for action, who, at in ev:
                        ui.timeline_entry("", title=action.replace(".", " › ").replace("_", " "), subtitle=f"{who} · {at.astimezone(IST):%d %b %H:%M}")


STAGES = [("draft", "Draft"), ("internal_review", "Internal review"), ("approved", "Approved"), ("ready_for_external_submission", "Ready to submit")]


def _stepper(status: str) -> None:
    """Horizontal lifecycle indicator for the bid workspace."""
    order = [k for k, _ in STAGES]
    cur = order.index("draft" if status == "changes_requested" else status) if (status in order or status == "changes_requested") else 0
    with ui.row().classes("w-full items-center no-wrap tp-card").style("padding:14px 20px;gap:0;overflow-x:auto"):
        for i, (key, label) in enumerate(STAGES):
            done, here = i < cur, i == cur
            with ui.row().classes("items-center no-wrap gap-2").style("flex:none"):
                with ui.element("div").style("width:26px;height:26px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:.75rem;font-weight:650;"
                                             + ("background:#2563EB;color:#fff" if done else "background:#EFF4FF;color:#1D4ED8;box-shadow:inset 0 0 0 2px #2563EB" if here
                                                else "background:#F2F4F7;color:#98A2B3")):
                    if done:
                        ui.icon("check", size="15px")
                    else:
                        ui.label(str(i + 1))
                ui.label(label + (" · changes requested" if here and status == "changes_requested" else "")).classes("text-weight-medium").style(
                    "font-size:.85rem;" + ("color:#101828" if done or here else "color:#98A2B3"))
            if i < len(STAGES) - 1:
                ui.element("div").classes("col").style(f"height:2px;min-width:24px;margin:0 14px;background:{'#2563EB' if done else '#EAECF0'};border-radius:2px")


def _tasks(pc, wid, members):
    @ui.refreshable
    def body():
        with session_scope() as db:
            tasks = [(t.id, t.title, t.description, t.status, t.priority, t.assignee_id, t.due_date, t.source, t.lock_version) for t in bids.list_tasks(db, pc.ctx, wid)]
        if not tasks:
            with ui.card().classes("tp-card w-full").props("flat"):
                empty_state("task_alt", "No tasks yet", "Remediation tasks are created automatically from eligibility gaps, or add your own above.")
        done_n = sum(1 for t in tasks if t[3] == "done")
        if tasks:
            with ui.row().classes("w-full items-center gap-3"):
                ui.label(f"{done_n} of {len(tasks)} tasks done").classes("tp-caption text-weight-medium")
                ui.linear_progress(value=done_n / len(tasks), show_value=False).classes("col").style("max-width:240px")
        for tid, title, desc, status, prio, assignee, due, source, lv in tasks:
            overdue = due and due < date.today() and status != "done"
            with ui.card().classes("tp-card w-full").props("flat").style("padding:10px 14px;gap:4px"):
                with ui.row().classes("w-full items-center justify-between no-wrap gap-2"):
                    with ui.row().classes("items-center gap-2 no-wrap col"):
                        ui.checkbox(value=status == "done", on_change=lambda e, tid=tid, lv=lv: run(lambda db: bids.update_task(db, pc.ctx, tid, {"status": "done" if e.value else "todo"}, lv), refresh=body.refresh)).set_enabled(pc.can(Perm.TASKS_WRITE))
                        with ui.column().classes("gap-0"):
                            ui.label(title).classes("text-body1 text-weight-medium" + (" text-strike tp-muted" if status == "done" else "")).style("" if status == "done" else "color:#101828")
                            with ui.row().classes("items-center gap-2"):
                                ui.badge(prio.capitalize(), color=PRIO_COLOR.get(prio, "grey-7")).props("outline").classes("tp-chip")
                                ui.label(source.replace("_", " ")).classes("tp-caption")
                                if due:
                                    with ui.row().classes("items-center gap-1 no-wrap"):
                                        ui.icon("event", size="14px").style(f"color:{'#D92D20' if overdue else '#98A2B3'}")
                                        ui.label(("Overdue · " if overdue else "Due ") + f"{due:%d %b}").classes("text-caption").style(f"color:{'#B42318' if overdue else '#667085'}")
                    if pc.can(Perm.TASKS_WRITE):
                        ui.select({"": "Unassigned", **members}, value=str(assignee) if assignee else "",
                                  on_change=lambda e, tid=tid, lv=lv: run(lambda db: bids.update_task(db, pc.ctx, tid, {"assignee_id": parse_uuid(e.value) if e.value else None}, lv), success="Assigned", refresh=body.refresh)).props("dense outlined").style("width:180px")
                        ui.select(["todo", "in_progress", "blocked", "done"], value=status,
                                  on_change=lambda e, tid=tid, lv=lv: run(lambda db: bids.update_task(db, pc.ctx, tid, {"status": e.value}, lv), refresh=body.refresh)).props("dense outlined").style("width:140px")
                if desc:
                    with ui.expansion("Details").classes("w-full"):
                        ui.label(desc).classes("text-body2").style("white-space:pre-wrap")
                        comments_box(pc, wid, tid)

    if pc.can(Perm.TASKS_WRITE):
        with ui.row().classes("w-full gap-2 items-center tp-card").style("padding:12px"):
            title = ui.input("New task").props("outlined dense").classes("col").style("min-width:220px")
            prio = ui.select(["low", "medium", "high", "urgent"], value="medium").props("outlined dense").style("width:120px")
            due = ui.input("Due (YYYY-MM-DD)").props("outlined dense").style("width:170px")

            def add():
                def do(db):
                    d = date.fromisoformat(due.value) if due.value else None
                    return bids.create_task(db, pc.ctx, wid, title=title.value or "", priority=prio.value, due_date=d)
                if run(do, success="Task added", refresh=body.refresh):
                    title.value = ""
            ui.button("Add task", icon="add", on_click=add).props("unelevated color=primary")
    body()


def comments_box(pc, wid, task_id):
    @ui.refreshable
    def body():
        with session_scope() as db:
            rows = [(c.body, c.author_id, c.created_at) for c in bids.comments(db, pc.ctx, wid, task_id=task_id)]
            names = {u.id: u.full_name for u in db.scalars(select(User).where(User.id.in_([r[1] for r in rows if r[1]])))}
        for b, a, at in rows:
            with ui.row().classes("w-full items-start no-wrap gap-2 q-py-xs"):
                ui.html(f'<div class="tp-avatar sm">{html.escape(initials(names.get(a, "—")))}</div>')
                with ui.column().classes("gap-0 col"):
                    with ui.row().classes("items-center gap-2"):
                        ui.label(names.get(a, "—")).classes("text-caption text-weight-medium").style("color:#101828")
                        ui.label(f"{at.astimezone(IST):%d %b %H:%M}").classes("tp-caption")
                    ui.label(b).classes("text-body2")
    body()
    if pc.can(Perm.BIDS_WRITE):
        with ui.row().classes("w-full no-wrap"):
            inp = ui.input("Write a comment…").props("dense outlined").classes("col")
            ui.button(icon="send", on_click=lambda: (run(lambda db: bids.add_comment(db, pc.ctx, wid, inp.value or "", task_id=task_id), refresh=body.refresh), inp.set_value(""))).props("flat dense")


SEV_COLOR = {"high": "negative", "medium": "warning", "low": "grey-7"}
PRIO_COLOR = {"urgent": "negative", "high": "orange", "medium": "info", "low": "grey-7"}


def _agent_panel(pc, wid, ws_status, on_drafts_changed):
    """Bid preparation agent (LangGraph): progress, suggested attachments, risk findings and the approval gate."""
    @ui.refreshable
    def panel():
        with session_scope() as db:
            r = bid_preparation.latest_run(db, pc.ctx, wid)
            info = (r.id, r.status, r.mode, r.history, r.result or {}, r.interrupt_payload, r.error) if r else None
        with ui.card().classes("tp-card tp-panel w-full").props("flat").style("background:linear-gradient(180deg,#F8FAFF 0%,#fff 70%);border-color:#D1E0FF"):
            with ui.row().classes("w-full items-center justify-between no-wrap gap-3"):
                with ui.row().classes("items-center no-wrap gap-3 col"):
                    with ui.element("div").classes("tp-logo").style("width:38px;height:38px;border-radius:11px"):
                        ui.icon("smart_toy", size="21px")
                    with ui.column().classes("gap-0 col"):
                        ui.label("Bid preparation agent").classes("tp-h2")
                        ui.label("Plans the document structure, drafts from verified evidence, checks for unsupported claims, suggests attachments "
                                 "and stops for your decision before anything is submitted for review.").classes("tp-caption")
                if pc.can(Perm.BIDS_WRITE) and ws_status in ("draft", "changes_requested") and (not info or info[1] not in ("running", "interrupted")):
                    ui.button("Run agent", icon="play_arrow", on_click=lambda: run(lambda db: bid_preparation.start(db, pc.ctx, wid), success="Agent started",
                                                                                     refresh=panel.refresh)).props("unelevated color=primary")
            if not info:
                return
            rid, status, mode, history, result, intr, err = info
            with ui.row().classes("items-center gap-2"):
                ui.badge(status.capitalize(), color={"completed": "positive", "interrupted": "warning", "failed": "negative"}.get(status, "info")).classes("tp-chip")
                ui.label(f"{mode} mode · " + " → ".join(h["node"].replace("_", " ") for h in history)).classes("text-caption tp-muted")
            if err:
                ui.label(err[:300]).classes("text-negative text-caption")
            summ = result.get("summary") or {}
            if summ:
                ui.label(("AI summary (claims verified against the draft)" if summ.get("mode") == "llm" else "Summary (rule-based)") + ": " + summ.get("text", "")).classes("text-body2")
            for f in result.get("findings") or []:
                with ui.row().classes("items-start no-wrap gap-2"):
                    ui.badge(f["severity"].capitalize(), color=SEV_COLOR.get(f["severity"], "grey-7")).classes("tp-chip")
                    ui.label(f["text"]).classes("text-body2")
            att = result.get("attachments") or []
            if att:
                with ui.expansion(f"Suggested attachments ({sum(1 for a in att if a['status'] == 'on_record')} on record, "
                                  f"{sum(1 for a in att if a['status'] == 'missing')} missing)", icon="attach_file").classes("w-full").style("background:#fff;border:1px solid #EAECF0"):
                    for a in att:
                        if a.get("document_version_id"):
                            ui.link(f"{a['requirement']}: {a['attach']}" + (f" p.{a['page']}" if a.get("page") else ""),
                                    f"/viewer/{a['document_version_id']}?page={a.get('page') or 1}").classes("tp-link text-caption")
                        else:
                            ui.label(f"{a['requirement']}: {a['attach']}").classes("text-caption " + ("text-negative" if a["status"] == "missing" else ""))
            if status == "interrupted" and intr and pc.can(Perm.BIDS_WRITE):
                with ui.row().classes("w-full items-center gap-2 q-mt-sm tp-card").style("padding:12px 14px;background:#FFFAEB;border-color:#FEDF89"):
                    ui.icon("front_hand", size="20px").style("color:#DC6803")
                    ui.label(intr.get("question", "Decision needed")).classes("text-weight-medium col").style("color:#101828")

                    def decide(d):
                        if run(lambda db: bid_preparation.resume(db, pc.ctx, rid, d), success="Decision recorded"):
                            ui.navigate.reload() if d == "submit" else (panel.refresh(), on_drafts_changed())
                    ui.button("Keep as draft", on_click=lambda: decide("keep_draft")).props("outline color=primary")
                    ui.button("Submit for internal review", on_click=lambda: decide("submit")).props("unelevated color=primary")
            if status == "running":
                ui.timer(2.0, lambda: (panel.refresh(), on_drafts_changed()), once=True)
    panel()


def _drafts(pc, wid, ws_status):
    @ui.refreshable
    def body():
        with session_scope() as db:
            drafts = [(d.id, d.kind, d.title, d.version_no, d.status, d.sections, d.generator, d.lock_version) for d in bids.drafts(db, pc.ctx, wid)]
        with ui.row().classes("w-full items-center justify-between gap-3"):
            ui.label("Every statement is drawn from your records with a [Source: …] citation. Unknown facts appear as [TO BE COMPLETED: …] — nothing is invented.").classes("tp-caption col")
            if pc.can(Perm.BIDS_WRITE):
                ui.button("Generate drafts from verified evidence", icon="auto_fix_high",
                          on_click=lambda: run(lambda db: generation.generate_drafts(db, pc.ctx, wid), success="Drafts generated", refresh=body.refresh)).props("unelevated color=primary")
        if not drafts:
            with ui.card().classes("tp-card w-full").props("flat"):
                empty_state("edit_note", "No drafts yet", "Generate drafts from verified evidence, or run the bid preparation agent.")
        for did, kind, title, vno, status, sections, gen, lv in drafts:
            with ui.card().classes("tp-card tp-panel w-full").props("flat"):
                with ui.row().classes("w-full items-center justify-between no-wrap gap-2"):
                    with ui.row().classes("items-center no-wrap gap-3"):
                        ui.icon("article").classes("tp-feature-icon tp-tone-primary").style("width:36px;height:36px;font-size:19px")
                        with ui.column().classes("gap-0"):
                            ui.label(title).classes("tp-h2")
                            ui.label(f"Version {vno} · {gen}").classes("tp-caption")
                    ui.badge(status.replace("_", " ").capitalize(), color=BID_STATUS_COLOR.get(status, "grey")).classes("tp-chip")
                editors = []
                for s in sections:
                    with ui.row().classes("items-center gap-2 q-mt-sm"):
                        ui.label(s["heading"]).classes("text-subtitle2")
                        if s.get("ai_generated"):
                            ui.badge("AI-written · claims checked", color="accent").classes("tp-chip")
                    ta = ui.textarea(value=s["body"]).props("outlined autogrow").classes("w-full")
                    ta.set_enabled(pc.can(Perm.BIDS_WRITE) and ws_status in ("draft", "changes_requested"))
                    editors.append((s, ta))
                if pc.can(Perm.BIDS_WRITE) and ws_status in ("draft", "changes_requested"):
                    ui.button("Save as new version", icon="save", on_click=lambda did=did, editors=editors, lv=lv: run(
                        lambda db: bids.edit_draft(db, pc.ctx, did, [{**s, "body": ta.value} for s, ta in editors], lv), success="Saved new version", refresh=body.refresh)).props("unelevated color=primary").classes("self-end")
    _agent_panel(pc, wid, ws_status, lambda: body.refresh())
    body()


def _boq(pc, wid, tid):
    @ui.refreshable
    def body():
        with session_scope() as db:
            from app.db.models import BoqImport

            imp = db.scalar(select(BoqImport).where(BoqImport.tender_id == tid, BoqImport.organization_id == pc.ctx.organization_id).order_by(BoqImport.created_at.desc()))
            if imp:
                items = [(str(i.id), i.row_no, i.item_code, i.description, i.unit, i.quantity, i.quoted_rate, i.rate_cell_locked, i.is_heading, i.is_subtotal, i.issues, i.amount_formula)
                         for i in boq.items(db, pc.ctx, imp.id)]
                scen = [(s.id, s.name, s.totals) for s in boq.scenarios(db, pc.ctx, imp.id)]
                imp_info = (imp.id, imp.sheet_name, imp.summary, imp.issues)
        if not imp:
            empty_state("table_view", "No BOQ analysed", "Attach the tender's BOQ workbook (XLSX) to the tender, then analyse it here.", "Analyse attached BOQ",
                        lambda: run(lambda db: boq.import_boq(db, pc.ctx, tid), success="BOQ analysed", refresh=body.refresh))
            return
        iid, sheet, summary, issues = imp_info
        with ui.element("div").classes("tp-kpi-grid w-full").style("grid-template-columns:repeat(auto-fit,minmax(150px,1fr))"):
            for k, v in summary.items():
                with ui.card().classes("tp-card").props("flat").style("padding:12px 16px;gap:2px"):
                    ui.label(k.replace("_", " ").capitalize()).classes("tp-caption")
                    ui.label(str(v)).classes("tp-num").style("font-weight:650;font-size:1.2rem;color:#101828")
        if issues:
            with ui.expansion(f"{len(issues)} issue(s) detected", icon="warning").classes("tp-card w-full"):
                for i in issues:
                    ui.label(f"{i['type'].replace('_', ' ')} — rows {', '.join(map(str, i['rows']))}: {i['description']}" + (f" (units: {', '.join(i['units'])})" if i.get("units") else "")).classes("text-body2")
        ui.label("Internal estimate — kept separate from the prescribed BOQ. Enter rates below; amounts use exact decimal arithmetic.").classes("text-caption tp-muted")
        rate_inputs = {}
        with ui.element("div").classes("w-full tp-card").style("max-height: 560px; overflow: auto"):
            with ui.grid(columns="60px 80px 1fr 70px 90px 140px").classes("w-full items-center").style("gap:0;row-gap:0"):
                for h in ("Row", "Item", "Description", "Unit", "Qty", "Rate (₹)"):
                    ui.label(h).classes("text-caption text-weight-bold").style("color:#475467;background:#F9FAFB;padding:10px 8px;border-bottom:1px solid #EAECF0;position:sticky;top:0;z-index:1")
                cell = "padding:6px 8px;border-bottom:1px solid #F2F4F7;min-height:44px;display:flex;align-items:center"
                for iid_, row, code, desc, unit, qty, rate, locked, heading, sub, its, formula in items:
                    bg = "background:#F9FAFB;" if heading or sub else ""
                    ui.label(str(row)).classes("text-caption tp-num tp-muted").style(cell + bg)
                    ui.label(code or "").classes("text-caption").style(cell + bg)
                    ui.label(desc).classes("text-body2 " + ("text-weight-bold" if heading or sub else "")).style(cell + bg).tooltip(", ".join(its) if its else (formula or ""))
                    ui.label(unit or ("—" if not (heading or sub) else "")).classes("text-caption" + (" text-negative" if "missing_unit" in its else "")).style(cell + bg)
                    ui.label(str(qty) if qty is not None else ("—" if not (heading or sub) else "")).classes("text-caption tp-num" + (" text-negative" if "missing_quantity" in its else "")).style(cell + bg)
                    if heading or sub:
                        ui.label("").style(cell + bg)
                    else:
                        with ui.element("div").style(cell):
                            rate_inputs[iid_] = ui.input(value=str(rate) if rate is not None else "").props("dense outlined" + (" readonly" if locked else "")).classes("w-full")
        with ui.row().classes("w-full items-center gap-2 tp-card").style("padding:12px"):
            name = ui.input("Scenario name", value="Base estimate").props("outlined dense")
            mk = ui.input("Markup %", value="10").props("outlined dense").style("width:110px")
            gst = ui.input("GST %", value="18").props("outlined dense").style("width:110px")
            ui.button("Calculate & save scenario", on_click=lambda: run(lambda db: boq.save_scenario(db, pc.ctx, iid, name.value, {k: v.value for k, v in rate_inputs.items() if v.value}, mk.value, gst.value),
                                                                        success="Scenario saved", refresh=body.refresh)).props("unelevated color=primary")
        for sid, sname, totals in scen:
            with ui.card().classes("tp-card w-full").props("flat").style("padding:14px 18px"):
                with ui.row().classes("w-full items-center justify-between no-wrap gap-3"):
                    with ui.column().classes("gap-1 col"):
                        with ui.row().classes("items-center gap-2"):
                            ui.label(sname).classes("tp-h2")
                            if not totals["complete"]:
                                ui.badge("Incomplete", color="warning").classes("tp-chip")
                            ui.label(f"{totals['priced_items']} priced · {totals['unpriced_items']} unpriced").classes("tp-caption")
                        ui.label(f"Subtotal {format_inr(Decimal(totals['subtotal']))} + GST {format_inr(Decimal(totals['gst']))}").classes("tp-caption tp-num")
                    with ui.column().classes("items-end gap-0"):
                        ui.label("Grand total").classes("tp-caption")
                        ui.label(format_inr(Decimal(totals["grand_total"]))).classes("tp-num").style("font-weight:650;font-size:1.15rem;color:#101828")

                    def export(sid=sid):
                        ver = run(lambda db: boq.export_priced_copy(db, pc.ctx, sid), success="Priced copy exported (formulas and locked cells preserved)")
                        if ver:
                            with session_scope() as db:
                                from app.services.documents import service as documents

                                url = documents.download_url(db, pc.ctx, ver.id)
                            ui.navigate.to(url, new_tab=True)
                    ui.button("Export priced copy (XLSX)", icon="download", on_click=export).props("dense outline color=primary")
        ui.label("Exports are internal copies; validate against the tender's prescribed format and portal rules before submission.").classes("text-caption tp-muted")
    body()


def _exports(pc, wid):
    @ui.refreshable
    def body():
        with session_scope() as db:
            arts = []
            for a in bids.artifacts(db, pc.ctx, wid):
                d = db.get(Document, a.document_id)
                arts.append((a.kind, a.format, d.title if d else "", d.current_version_id if d else None, a.created_at))
        with ui.row().classes("w-full items-center justify-between gap-3"):
            ui.label("Generates the technical response (DOCX), capability summary, compliance matrix and checklist (XLSX), evidence and risk reports (PDF), "
                     "and a ZIP with the exact supporting document versions plus a SHA-256 manifest.").classes("tp-caption col")
            if pc.can(Perm.BIDS_WRITE):
                ui.button("Export submission package", icon="inventory_2", on_click=lambda: run(lambda db: generation.export_package(db, pc.ctx, wid),
                                                                                               success="Package exported", refresh=body.refresh)).props("unelevated color=primary")
        if not arts:
            with ui.card().classes("tp-card w-full").props("flat"):
                empty_state("inventory_2", "No exports yet", "Export the submission package to generate reviewable documents.")
            return
        with ui.card().classes("tp-card w-full").props("flat").style("padding:4px 18px;gap:0"):
            for kind, fmt, title, vid, at in arts:
                with ui.row().classes("w-full items-center justify-between no-wrap tp-row-line"):
                    with ui.row().classes("items-center gap-3 no-wrap"):
                        tone = {"pdf": "rose", "xlsx": "green", "docx": "primary", "zip": "amber"}.get(fmt.lower(), "grey")
                        ui.label(fmt.upper()).classes(f"tp-tone-{tone}").style("font-size:.65rem;font-weight:700;border-radius:6px;padding:4px 0;width:42px;text-align:center")
                        ui.label(title).classes("text-body2 text-weight-medium").style("color:#101828")
                    with ui.row().classes("items-center gap-2"):
                        ui.label(at.astimezone(IST).strftime("%d %b %H:%M")).classes("text-caption tp-muted")
                        if vid:
                            download_button(pc, vid)
    body()


@ui.page("/team")
def team(request: Request):
    with shell(request, "/team") as pc:
        if not pc:
            return
        with page_header("Team", "Members, roles and pending invitations for this organization."):
            pass

        @ui.refreshable
        def body():
            with session_scope() as db:
                ms = [(m.id, u.full_name, u.email, m.role, m.is_active) for m, u in org_service.list_members(db, pc.ctx)]
                invs = [(i.id, i.email, i.role, i.expires_at) for i in org_service.list_invitations(db, pc.ctx)] if pc.can(Perm.MEMBERS_MANAGE) else []
            with panel("Members", f"{sum(1 for m in ms if m[4])} active member(s)", icon="groups"):
                for mid, nm, email, role, active in ms:
                    with ui.row().classes("w-full items-center justify-between no-wrap tp-row-line gap-3"):
                        with ui.row().classes("items-center no-wrap gap-3 col").style("min-width:0" + ("" if active else ";opacity:.55")):
                            ui.html(f'<div class="tp-avatar">{html.escape(initials(nm))}</div>')
                            with ui.column().classes("gap-0 col").style("min-width:0"):
                                with ui.row().classes("items-center gap-2"):
                                    ui.label(nm).classes("text-weight-medium").style("color:#101828")
                                    if not active:
                                        ui.badge("Removed", color="grey-6").classes("tp-chip")
                                ui.label(email).classes("tp-caption ellipsis")
                        if pc.can(Perm.MEMBERS_MANAGE) and active:
                            with ui.row().classes("items-center gap-1"):
                                ui.select({r.value: ROLE_LABELS[r] for r in Role}, value=role,
                                          on_change=lambda e, mid=mid: run(lambda db: org_service.change_member_role(db, pc.ctx, mid, e.value), success="Role updated", refresh=body.refresh)).props("dense outlined").style("width:200px")
                                ui.button(icon="person_remove", on_click=lambda mid=mid: run(lambda db: org_service.remove_member(db, pc.ctx, mid), success="Member removed", refresh=body.refresh)).props("flat dense color=negative")
                        else:
                            ui.badge(ROLE_LABELS.get(role, role), color="grey-7").props("outline").classes("tp-chip")
            if pc.can(Perm.MEMBERS_MANAGE):
                with panel("Invite a teammate", "They'll receive an email link to join this organization.", icon="person_add"):
                    with ui.row().classes("w-full gap-2 items-center"):
                        email = ui.input("Email").props("outlined dense").classes("col")
                        role = ui.select({r.value: ROLE_LABELS[r] for r in Role if r != Role.OWNER or pc.ctx.role == Role.OWNER}, value="contributor").props("outlined dense").style("width:220px")

                        def invite():
                            res = run(lambda db: org_service.invite_member(db, pc.ctx, email.value or "", role.value), success="Invitation sent", refresh=body.refresh)
                            if res and get_settings().environment == "development":
                                ui.notify(f"Dev invite link: {get_settings().base_url}/invite?token={res[1]}", type="info", timeout=0, close_button=True, multi_line=True)
                        ui.button("Send invite", icon="send", on_click=invite).props("unelevated color=primary")
                    if invs:
                        ui.label("Pending invitations").classes("tp-nav-section").style("padding:8px 0 0")
                    for iid, em, rl, exp in invs:
                        with ui.row().classes("w-full items-center justify-between no-wrap tp-row-line"):
                            with ui.row().classes("items-center gap-2"):
                                ui.icon("mail_outline", size="18px").style("color:#98A2B3")
                                ui.label(em).classes("text-body2 text-weight-medium").style("color:#101828")
                                ui.badge(ROLE_LABELS.get(rl, rl), color="grey-7").props("outline").classes("tp-chip")
                                ui.label(f"expires {exp.astimezone(IST):%d %b}").classes("tp-caption")
                            ui.button("Revoke", on_click=lambda iid=iid: run(lambda db: org_service.revoke_invitation(db, pc.ctx, iid), success="Revoked", refresh=body.refresh)).props("flat dense color=negative")
        body()


@ui.page("/settings")
def settings_page(request: Request):
    with shell(request, "/settings") as pc:
        if not pc:
            return
        with page_header("Settings", "Organization details, document branding, notifications, API access, plan and integrations."):
            pass
        with session_scope() as db:
            org = db.get(Organization, pc.ctx.organization_id)
            sub, plan = entitlements.get_subscription(db, pc.ctx.organization_id)
            limits = entitlements.limits_for(db, pc.ctx.organization_id)
            usage = {m: entitlements.get_usage(db, pc.ctx.organization_id, m) for m in ("documents_processed", "tenders_analysed", "ai_tokens")}
            storage = entitlements.storage_used(db, pc.ctx.organization_id)
            org_info = (org.name, org.settings or {}, sub.status if sub else None, plan.name if plan else None, sub.pending_plan_code if sub else None,
                        sub.current_period_end if sub else None)
            from app.db.models import ApiToken

            tokens = [(t.id, t.name, t.token_prefix, t.scopes, t.revoked_at, t.expires_at) for t in db.scalars(select(ApiToken).where(ApiToken.organization_id == pc.ctx.organization_id, ApiToken.user_id == pc.ctx.user_id))]
        name, osettings, sub_status, plan_name, pending, period_end = org_info
        with ui.element("div").classes("tp-grid-2 w-full"):
            with ui.column().classes("w-full gap-4"):
                with panel("Organization", icon="domain"):
                    nm = ui.input("Name", value=name).props("outlined dense").classes("w-full")
                    lh = ui.input("Letterhead name on generated documents", value=osettings.get("letterhead_name", "")).props("outlined dense").classes("w-full")
                    pol = ui.select({"reopen_approvals_on_mandatory_change": "Reopen approvals when a corrigendum changes a mandatory condition",
                                     "flag_only": "Only flag affected items"}, value=osettings.get("amendment_policy", "reopen_approvals_on_mandatory_change"), label="Corrigendum policy").props("outlined dense").classes("w-full")
                    if pc.can(Perm.ORG_MANAGE):
                        ui.button("Save changes", on_click=lambda: run(lambda db: org_service.update_org_settings(db, pc.ctx, name=nm.value, settings={"letterhead_name": lh.value, "amendment_policy": pol.value}), success="Saved")).props("unelevated color=primary").classes("self-end")
                _template_card(pc, osettings)
                with panel("Notifications", "Choose which events also send an email", icon="notifications_none"):
                    for kind, label in [("deadline", "Deadline reminders"), ("document_expiry", "Document expiry warnings"), ("task_assigned", "Task assignments"),
                                        ("review_requested", "Review requests"), ("approval", "Approvals"), ("amendment", "Tender amendments"), ("job_failed", "Failed processing")]:
                        with ui.row().classes("w-full items-center justify-between no-wrap tp-row-line"):
                            ui.label(label).classes("text-body2").style("color:#101828")
                            em = ui.switch("Email", value=True).props("dense")
                            em.on_value_change(lambda e, kind=kind: run(lambda db: notifications.set_preference(db, pc.ctx, kind, True, e.value), success="Preference saved"))
                    ui.label("Email delivery status is recorded only as the provider reports it; without SMTP configured, emails are not sent." if not get_settings().smtp_host else "SMTP provider configured.").classes("text-caption tp-muted")
                with panel("API tokens", "For MCP & integrations. Tokens act as you; add mcp:write only if an external agent may change data.", icon="key"):
                    for tid_, tname, prefix, scopes, revoked, exp in tokens:
                        with ui.row().classes("w-full items-center justify-between no-wrap tp-row-line"):
                            with ui.column().classes("gap-0 col"):
                                with ui.row().classes("items-center gap-2"):
                                    ui.label(tname).classes("text-weight-medium").style("color:#101828")
                                    if revoked:
                                        ui.badge("Revoked", color="grey-6").classes("tp-chip")
                                ui.label(f"{prefix}… · {', '.join(scopes) or 'role permissions'}").classes("tp-caption").style("font-family:ui-monospace,monospace")
                            if not revoked:
                                ui.button("Revoke", on_click=lambda tid_=tid_: (run(lambda db: auth_service.revoke_api_token(db, pc.ctx, tid_), success="Revoked"), ui.navigate.reload())).props("flat dense color=negative")
                    with ui.row().classes("w-full gap-2 items-center"):
                        tn = ui.input("Token name").props("outlined dense").classes("col")
                        wr = ui.checkbox("mcp:write")

                        def mk():
                            res = run(lambda db: auth_service.create_api_token(db, pc.ctx, tn.value or "Token", ["mcp:write", "*"] if wr.value else []))
                            if res:
                                with ui.dialog() as dlg, ui.card().style("width:560px"):
                                    ui.icon("key").classes("tp-feature-icon tp-tone-amber")
                                    ui.label("Copy your token now — it won't be shown again.").classes("tp-h1").style("font-size:1.15rem")
                                    ui.code(res[1]).classes("w-full")
                                    ui.label(f"MCP endpoint: {get_settings().base_url}/mcp/  (Authorization: Bearer <token>)").classes("text-caption")
                                    ui.button("Done", on_click=lambda: (dlg.close(), ui.navigate.reload())).props("unelevated color=primary").classes("self-end")
                                dlg.open()
                        if pc.can(Perm.API_TOKENS):
                            ui.button("Create token", on_click=mk).props("unelevated color=primary")
            with ui.column().classes("w-full gap-4"):
                with panel("Plan & usage", icon="data_usage"):
                    with ui.row().classes("items-center gap-2"):
                        ui.label(plan_name or "—").classes("text-weight-medium").style("font-size:1.05rem;color:#101828")
                        if sub_status:
                            ui.badge(sub_status.capitalize(), color="positive" if sub_status == "active" else "info").classes("tp-chip")
                    if period_end:
                        ui.label(f"Current period ends {period_end.astimezone(IST):%d %b %Y}").classes("tp-caption")
                    if pending:
                        ui.label(f"Requested change to {pending} — pending administrator (no payment processor configured).").classes("text-caption text-warning")
                    for metric, used in usage.items():
                        lim = limits.get(f"{metric}_per_month")
                        with ui.column().classes("w-full gap-1"):
                            with ui.row().classes("w-full justify-between"):
                                ui.label(metric.replace("_", " ").capitalize()).classes("text-body2").style("color:#101828")
                                ui.label(f"{used:,} / {lim:,} this month" if lim else f"{used:,}").classes("tp-caption tp-num")
                            if lim:
                                ui.linear_progress(value=min(1, used / lim), show_value=False, color="negative" if used / lim > .9 else "primary")
                    with ui.column().classes("w-full gap-1"):
                        with ui.row().classes("w-full justify-between"):
                            ui.label("Storage").classes("text-body2").style("color:#101828")
                            ui.label(f"{storage / 1024**2:.1f} MB / {limits.get('storage_bytes', 0) / 1024**3:.0f} GB").classes("tp-caption tp-num")
                        if limits.get("storage_bytes"):
                            ui.linear_progress(value=min(1, storage / limits["storage_bytes"]), show_value=False)
                    if pc.can(Perm.ORG_BILLING):
                        with ui.row().classes("gap-2 q-mt-sm"):
                            for code in ("starter", "professional", "enterprise"):
                                ui.button(f"Request {code}", on_click=lambda code=code: run(lambda db: entitlements.request_plan_change(db, pc.ctx.organization_id, code, pc.ctx.user_id), success="Request recorded")).props("dense outline color=primary")
                with panel("Integrations", icon="extension"):
                    for key, c in CONNECTORS.items():
                        with ui.row().classes("w-full items-center justify-between no-wrap tp-row-line gap-3"):
                            with ui.column().classes("gap-0 col"):
                                ui.label(c.name).classes("text-weight-medium").style("color:#101828")
                                ui.label(c.description).classes("tp-caption")
                            ui.badge("Available" if c.available() else "Not configured", color="positive" if c.available() else "grey-6").classes("tp-chip")
                _feeds_card(pc)
                if pc.can(Perm.ORG_DELETE):
                    with ui.card().classes("tp-card tp-panel w-full").props("flat").style("border-color:#FECDCA;background:#FFFBFA"):
                        with ui.row().classes("items-center gap-3 no-wrap"):
                            ui.icon("warning_amber").classes("tp-panel-icon").style("background:#FEE4E2;color:#D92D20")
                            ui.label("Danger zone").classes("tp-h2").style("color:#B42318")
                        ui.label("Request deletion of this organization. Data and stored files are purged after a 30-day grace period.").classes("tp-caption")
                        ui.button("Request deletion", icon="delete_forever", on_click=lambda: run(lambda db: org_service.request_deletion(db, pc.ctx), success="Deletion requested")).props("outline color=negative").classes("self-start")


_ = (Decimal, format_inr_short)


FEED_STATUS_COLOR = {"active": "positive", "paused": "grey-6", "error": "negative"}


def _feeds_card(pc):
    """Licensed / open-data feed subscriptions and the portal email-alert inbound address."""
    from app.services.tenders import feeds as feedsvc

    manage = pc.can(Perm.INTEGRATIONS_MANAGE)

    @ui.refreshable
    def body():
        with session_scope() as db:
            rows = [feedsvc.feed_view(c) for c in feedsvc.list_feeds(db, pc.ctx)]
            mail = feedsvc.email_view(feedsvc.email_settings(db, pc.ctx))
        ui.label("Feeds").classes("text-weight-medium q-mt-xs").style("color:#101828")
        if not rows:
            ui.label("No feeds yet. Add a licensed provider's feed URL or an open-data OCDS publication.").classes("tp-caption")
        for f in rows:
            with ui.row().classes("w-full items-center justify-between no-wrap tp-row-line gap-3"):
                with ui.column().classes("gap-0 col").style("min-width:0"):
                    ui.label(f["name"]).classes("text-weight-medium").style("color:#101828")
                    last = f["last_result"] or {}
                    meta = f"{f['format'].upper()} · every {f['poll_minutes']} min · " + (
                        f"last sync {f['last_sync_at'][:16].replace('T', ' ')} UTC: {last.get('created', 0)} new, {last.get('duplicates', 0)} already known"
                        if f["last_sync_at"] else "not synced yet")
                    ui.label(meta).classes("tp-caption")
                    if f["last_error"]:
                        ui.label(f["last_error"]).classes("text-caption").style("color:#B42318")
                ui.badge(f["status"], color=FEED_STATUS_COLOR.get(f["status"], "grey-6")).classes("tp-chip")
                fid = parse_uuid(f["id"])

                def poll(fid=fid):
                    res = run(lambda db: feedsvc.poll_feed(db, pc.ctx, fid))
                    if res is not None:
                        ui.notify(f"{len(res.created)} new tender(s), {res.duplicates} already known", type="positive")
                    body.refresh()
                ui.button(icon="sync", on_click=poll).props("flat round dense").tooltip("Poll now")
                if manage:
                    paused = f["status"] == "paused"
                    ui.button(icon="play_arrow" if paused else "pause",
                              on_click=lambda fid=fid, paused=paused: run(lambda db: feedsvc.set_feed_enabled(db, pc.ctx, fid, paused), refresh=body.refresh)
                              ).props("flat round dense").tooltip("Resume" if paused else "Pause")
                    ui.button(icon="delete_outline", on_click=lambda fid=fid: run(lambda db: feedsvc.delete_feed(db, pc.ctx, fid), success="Feed removed", refresh=body.refresh)
                              ).props("flat round dense color=negative").tooltip("Remove")
        if manage:
            with ui.expansion("Add a feed", icon="add").classes("w-full").style("border:1px solid #EAECF0"):
                name = ui.input("Name (e.g. provider or dataset)").props("outlined dense").classes("w-full")
                url = ui.input("Feed URL (https)").props("outlined dense").classes("w-full")
                with ui.row().classes("w-full gap-2 no-wrap"):
                    fmt = ui.select({"auto": "Detect automatically", "ocds": "OCDS (JSON)", "rss": "RSS / Atom", "json": "JSON list", "csv": "CSV"},
                                    value="auto", label="Format").props("outlined dense").classes("col")
                    every = ui.select({m: (f"{m} min" if m < 60 else f"{m // 60} h") for m in feedsvc.POLL_CHOICES}, value=60,
                                      label="Check every").props("outlined dense").classes("col")
                with ui.row().classes("w-full gap-2 no-wrap"):
                    hname = ui.input("Auth header (optional)", value="Authorization").props("outlined dense").classes("col")
                    hval = ui.input("Header value", password=True, password_toggle_button=True).props("outlined dense").classes("col")
                lic = ui.input("Licence / terms note (optional)").props("outlined dense").classes("w-full")
                ui.label("Only add feeds you are licensed or permitted to use. Values are fetched over https and stored as discovered tenders.").classes("tp-caption")

                def add():
                    ok = run(lambda db: feedsvc.create_feed(db, pc.ctx, name=name.value or "", url=url.value or "", fmt=fmt.value, poll_minutes=int(every.value),
                                                           auth_header_name=hname.value or None, auth_header_value=hval.value or None,
                                                           licence_note=lic.value or None), success="Feed added")
                    if ok is not None:
                        body.refresh()
                ui.button("Add feed", on_click=add).props("unelevated color=primary dense").classes("self-end")

        ui.separator().classes("q-my-sm")
        ui.label("Portal email alerts").classes("text-weight-medium").style("color:#101828")
        ui.label("Subscribe to CPPP, GeM or state-portal alerts with your own portal account, then forward those emails to your private "
                 "address below (most mail services can auto-forward). Each tender reference in an alert becomes a discovered tender.").classes("tp-caption")
        if mail.get("enabled"):
            last = mail.get("last_result") or {}
            ui.label(f"Enabled · address ends …{mail.get('token_hint')}" + (f" · last alert {mail['last_received_at'][:16].replace('T', ' ')} UTC: "
                     f"{last.get('created', 0)} new" if mail.get("last_received_at") else " · no alerts received yet")).classes("tp-caption")
            if mail.get("allowed_sender_domains"):
                ui.label("Accepted senders: " + ", ".join(mail["allowed_sender_domains"])).classes("tp-caption")
        else:
            ui.label("Not enabled.").classes("tp-caption")
        if manage:
            senders = ui.input("Accept alerts only from these sender domains (optional, comma separated)",
                               value=", ".join(mail.get("allowed_sender_domains") or [])).props("outlined dense").classes("w-full")

            def enable():
                doms = [d for d in (senders.value or "").replace(";", ",").split(",") if d.strip()]
                res = run(lambda db: feedsvc.enable_email_alerts(db, pc.ctx, doms))
                if res is None:
                    return
                addr_input.value = f"{get_settings().base_url.rstrip('/')}/api/v1/inbound/email/{res[1]}"
                addr_dialog.open()
                body.refresh()
            with ui.row().classes("gap-2"):
                ui.button("Generate new address" if mail.get("enabled") else "Enable email alerts", icon="alternate_email", on_click=enable).props("outline color=primary dense")
                if mail.get("enabled"):
                    ui.button("Disable", on_click=lambda: run(lambda db: feedsvc.disable_email_alerts(db, pc.ctx), success="Email alerts disabled", refresh=body.refresh)).props("flat dense color=negative")

    # Outside the refreshable body, so refreshing the panel cannot delete the dialog that shows the one-time address.
    with ui.dialog() as addr_dialog, ui.card().classes("tp-card").style("max-width:640px"):
        ui.label("Your private inbound address").classes("tp-h2")
        ui.label("Copy it now. It is shown only once. Point your mail service's inbound webhook (raw MIME) at it, "
                 "or use it with an email-to-webhook forwarder. Anyone with this address can add discovered tenders, so keep it private; "
                 "generating a new one disables the old one.").classes("tp-caption")
        addr_input = ui.input().props("outlined dense readonly").classes("w-full")
        ui.button("Done", on_click=lambda: (addr_input.set_value(""), addr_dialog.close())).props("unelevated color=primary").classes("self-end")

    with panel("Tender feeds & email alerts", "Authorised ways for tenders to arrive automatically. TenderPilot never scrapes portals, "
               "logs in for you or solves CAPTCHAs.", icon="rss_feed"):
        body()


def _template_card(pc, osettings):
    """Branding + organization-authored standard sections used in generated bid documents."""
    b = osettings.get("branding") or {}
    secs = list(((osettings.get("template") or {}).get("standard_sections")) or [])
    editable = pc.can(Perm.ORG_MANAGE)
    with panel("Document template & branding", "Applied to generated DOCX, XLSX and PDF files. Standard sections are your own approved text (e.g. quality policy, "
               "after-sales support), inserted verbatim into technical responses.", icon="palette"):
        lh = ui.input("Letterhead name", value=b.get("letterhead_name") or osettings.get("letterhead_name", "")).props("outlined dense").classes("w-full")
        addr = ui.input("Address line", value=b.get("address_line", "")).props("outlined dense").classes("w-full")
        foot = ui.input("Footer text", value=b.get("footer_text", "")).props("outlined dense").classes("w-full")
        color = ui.color_input("Brand colour", value=b.get("primary_color", "#1F2A44")).props("outlined dense").classes("w-full")
        editors = []
        for i in range(3):
            cur = secs[i] if i < len(secs) else {}
            with ui.expansion(f"Standard section {i + 1}" + (f": {cur['heading']}" if cur.get("heading") else ""), icon="notes").classes("w-full").style("border:1px solid #EAECF0"):
                h = ui.input("Heading", value=cur.get("heading", "")).props("outlined dense").classes("w-full")
                body = ui.textarea("Text", value=cur.get("body", "")).props("outlined autogrow").classes("w-full")
                editors.append((h, body))
        for el in [lh, addr, foot, color] + [e for pair in editors for e in pair]:
            el.set_enabled(editable)
        if editable:
            ui.button("Save template", on_click=lambda: run(lambda db: org_service.update_document_template(
                db, pc.ctx, branding_in={"letterhead_name": lh.value, "address_line": addr.value, "footer_text": foot.value, "primary_color": color.value},
                standard_sections=[{"heading": h.value, "body": bd.value} for h, bd in editors]), success="Template saved")).props("unelevated color=primary").classes("self-end")


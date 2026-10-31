"""Bid workspaces, team administration and settings."""

from __future__ import annotations

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
from app.ui.themes.theme import empty_state

BID_STATUS_COLOR = {"draft": "grey-7", "internal_review": "info", "changes_requested": "warning", "approved": "positive", "ready_for_external_submission": "secondary"}
NEXT_ACTIONS = {"draft": ["submit_for_review"], "changes_requested": ["submit_for_review"], "internal_review": ["approve", "request_changes"],
                "approved": ["mark_ready", "reopen"], "ready_for_external_submission": ["reopen"]}


@ui.page("/bids")
def bids_list(request: Request):
    with shell(request, "/bids") as pc:
        if not pc:
            return
        ui.label("Bid Workspaces").classes("tp-h1")
        with session_scope() as db:
            rows = [(w.id, w.name, w.status, t.title, t.submission_deadline, t.estimated_value, c) for w, t, c in bids.list_workspaces(db, pc.ctx)]
        if not rows:
            with ui.card().classes("tp-card w-full"):
                empty_state("workspaces", "No bid workspaces", "Create a workspace from a tender you decide to pursue.", "Go to tenders", lambda: ui.navigate.to("/tenders"))
            return
        for wid, name, status, title, dl, val, c in rows:
            with ui.card().classes("tp-card w-full q-pa-md cursor-pointer").on("click", lambda wid=wid: ui.navigate.to(f"/bids/{wid}")):
                with ui.row().classes("w-full items-center justify-between no-wrap"):
                    with ui.column().classes("gap-0"):
                        ui.label(title).classes("text-subtitle1 text-weight-medium")
                        ui.label(f"{format_inr_short(val) if val else '—'} · closes {dl.astimezone(IST):%d %b %Y %H:%M} IST" if dl else "—").classes("tp-muted")
                    with ui.row().classes("items-center gap-3"):
                        ui.label(f"{c['open']} open · {c['done']} done tasks").classes("tp-muted")
                        ui.badge(status.replace("_", " "), color=BID_STATUS_COLOR.get(status, "grey"))


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
        with ui.row().classes("w-full items-start justify-between no-wrap"):
            with ui.column().classes("gap-1"):
                ui.link(f"← {ttitle}", f"/tenders/{tid}").classes("tp-link text-caption")
                ui.label(name).classes("tp-h1")
                with ui.row().classes("items-center gap-2"):
                    ui.badge(status.replace("_", " "), color=BID_STATUS_COLOR.get(status, "grey"))
                    ui.label(f"{ref or ''} · submission {dl.astimezone(IST):%d %b %Y %H:%M} IST" if dl else "").classes("tp-muted")
            with ui.row().classes("gap-2"):
                ui.button("Ask copilot", icon="auto_awesome", on_click=lambda: ui.navigate.to(f"/copilot?tender={tid}")).props("outline color=primary")
                for action in NEXT_ACTIONS.get(status, []):
                    needed = {"submit_for_review": Perm.BIDS_WRITE, "approve": Perm.BIDS_APPROVE, "request_changes": Perm.BIDS_APPROVE, "mark_ready": Perm.BIDS_APPROVE, "reopen": Perm.BIDS_MANAGE}[action]
                    if pc.can(needed):
                        ui.button(action.replace("_", " ").capitalize(), on_click=lambda action=action: approval_dialog(action)).props(
                            "unelevated color=secondary" if action in ("approve", "submit_for_review", "mark_ready") else "outline color=primary")
        if status == "ready_for_external_submission":
            ui.label("This bid is approved and packaged. Submit it yourself through the official procurement portal; TenderPilot does not submit bids or apply digital signatures.").classes("tp-quote")

        def approval_dialog(action):
            with ui.dialog() as dlg, ui.card().style("width:480px"):
                ui.label(action.replace("_", " ").capitalize()).classes("tp-h2")
                note = ui.textarea("Note" + (" (required)" if action == "request_changes" else "")).props("outlined").classes("w-full")

                def go():
                    if run(lambda db: bids.approval_action(db, pc.ctx, wid, action, note.value), success="Recorded"):
                        dlg.close()
                        ui.navigate.reload()
                with ui.row():
                    ui.button("Confirm", on_click=go).props("unelevated color=primary")
                    ui.button("Cancel", on_click=dlg.close).props("flat")
            dlg.open()

        tab_q = request.query_params.get("tab", "tasks")
        with ui.tabs().classes("w-full") as tabs:
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
                ui.label("Lifecycle: Draft → Internal review → Changes requested → Approved → Ready for external submission. Approvers cannot approve their own drafts.").classes("tp-muted")
                for action, frm, to, dv, who, note, at in rows:
                    ui.label(f"{at.astimezone(IST):%d %b %H:%M} · {who} · {action.replace('_', ' ')} ({frm} → {to}) {'· draft v' + str(dv) if dv else ''} {('— ' + note) if note else ''}").classes("text-body2")
                if not rows:
                    ui.label("No approval activity yet.").classes("tp-muted")
            with ui.tab_panel(tb["documents"]):
                _exports(pc, wid)
            with ui.tab_panel(tb["activity"]):
                with session_scope() as db:
                    ev = [(e.action, members.get(str(e.actor_id), "system"), e.created_at) for e in bids.timeline(db, pc.ctx, wid)]
                with ui.timeline(side="right").classes("w-full"):
                    for action, who, at in ev:
                        ui.timeline_entry("", title=action.replace(".", " › ").replace("_", " "), subtitle=f"{who} · {at.astimezone(IST):%d %b %H:%M}")


def _tasks(pc, wid, members):
    @ui.refreshable
    def body():
        with session_scope() as db:
            tasks = [(t.id, t.title, t.description, t.status, t.priority, t.assignee_id, t.due_date, t.source, t.lock_version) for t in bids.list_tasks(db, pc.ctx, wid)]
        if not tasks:
            ui.label("No tasks yet. Remediation tasks are created automatically from eligibility gaps.").classes("tp-muted")
        for tid, title, desc, status, prio, assignee, due, source, lv in tasks:
            overdue = due and due < date.today() and status != "done"
            with ui.card().classes("tp-card w-full q-pa-sm"):
                with ui.row().classes("w-full items-center justify-between no-wrap"):
                    with ui.row().classes("items-center gap-2 no-wrap col"):
                        ui.checkbox(value=status == "done", on_change=lambda e, tid=tid, lv=lv: run(lambda db: bids.update_task(db, pc.ctx, tid, {"status": "done" if e.value else "todo"}, lv), refresh=body.refresh)).set_enabled(pc.can(Perm.TASKS_WRITE))
                        with ui.column().classes("gap-0"):
                            ui.label(title).classes("text-body1" + (" text-strike tp-muted" if status == "done" else ""))
                            ui.label(f"{source} · {prio} priority" + (f" · due {due:%d %b}" if due else "")).classes("text-caption " + ("text-negative" if overdue else "tp-muted"))
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
        with ui.row().classes("w-full gap-2 items-center"):
            title = ui.input("New task").props("outlined dense").classes("col")
            prio = ui.select(["low", "medium", "high", "urgent"], value="medium").props("outlined dense").style("width:120px")
            due = ui.input("Due (YYYY-MM-DD)").props("outlined dense").style("width:170px")

            def add():
                def do(db):
                    d = date.fromisoformat(due.value) if due.value else None
                    return bids.create_task(db, pc.ctx, wid, title=title.value or "", priority=prio.value, due_date=d)
                if run(do, success="Task added", refresh=body.refresh):
                    title.value = ""
            ui.button("Add", on_click=add).props("unelevated color=primary")
    body()


def comments_box(pc, wid, task_id):
    @ui.refreshable
    def body():
        with session_scope() as db:
            rows = [(c.body, c.author_id, c.created_at) for c in bids.comments(db, pc.ctx, wid, task_id=task_id)]
            names = {u.id: u.full_name for u in db.scalars(select(User).where(User.id.in_([r[1] for r in rows if r[1]])))}
        for b, a, at in rows:
            ui.label(f"{names.get(a, '—')} · {at.astimezone(IST):%d %b %H:%M}: {b}").classes("text-caption")
    body()
    if pc.can(Perm.BIDS_WRITE):
        with ui.row().classes("w-full no-wrap"):
            inp = ui.input("Comment").props("dense outlined").classes("col")
            ui.button(icon="send", on_click=lambda: (run(lambda db: bids.add_comment(db, pc.ctx, wid, inp.value or "", task_id=task_id), refresh=body.refresh), inp.set_value(""))).props("flat dense")


SEV_COLOR = {"high": "negative", "medium": "warning", "low": "grey-7"}


def _agent_panel(pc, wid, ws_status, on_drafts_changed):
    """Bid preparation agent (LangGraph): progress, suggested attachments, risk findings and the approval gate."""
    @ui.refreshable
    def panel():
        with session_scope() as db:
            r = bid_preparation.latest_run(db, pc.ctx, wid)
            info = (r.id, r.status, r.mode, r.history, r.result or {}, r.interrupt_payload, r.error) if r else None
        with ui.card().classes("tp-card w-full q-pa-md"):
            with ui.row().classes("w-full items-center justify-between"):
                with ui.column().classes("gap-0"):
                    ui.label("Bid preparation agent").classes("tp-h2")
                    ui.label("Plans the document structure, drafts from verified evidence, checks for unsupported claims, suggests attachments "
                             "and stops for your decision before anything is submitted for review.").classes("text-caption tp-muted")
                if pc.can(Perm.BIDS_WRITE) and ws_status in ("draft", "changes_requested") and (not info or info[1] not in ("running", "interrupted")):
                    ui.button("Run agent", icon="smart_toy", on_click=lambda: run(lambda db: bid_preparation.start(db, pc.ctx, wid), success="Agent started",
                                                                                     refresh=panel.refresh)).props("unelevated color=secondary")
            if not info:
                return
            rid, status, mode, history, result, intr, err = info
            with ui.row().classes("items-center gap-2"):
                ui.badge(status, color={"completed": "positive", "interrupted": "warning", "failed": "negative"}.get(status, "info"))
                ui.label(f"{mode} mode · " + " → ".join(h["node"].replace("_", " ") for h in history)).classes("text-caption tp-muted")
            if err:
                ui.label(err[:300]).classes("text-negative text-caption")
            summ = result.get("summary") or {}
            if summ:
                ui.label(("AI summary (claims verified against the draft)" if summ.get("mode") == "llm" else "Summary (rule-based)") + ": " + summ.get("text", "")).classes("text-body2")
            for f in result.get("findings") or []:
                ui.label(f"● {f['text']}").classes(f"text-caption text-{SEV_COLOR.get(f['severity'], 'grey-7')}")
            att = result.get("attachments") or []
            if att:
                with ui.expansion(f"Suggested attachments ({sum(1 for a in att if a['status'] == 'on_record')} on record, "
                                  f"{sum(1 for a in att if a['status'] == 'missing')} missing)").classes("w-full"):
                    for a in att:
                        if a.get("document_version_id"):
                            ui.link(f"{a['requirement']}: {a['attach']}" + (f" p.{a['page']}" if a.get("page") else ""),
                                    f"/viewer/{a['document_version_id']}?page={a.get('page') or 1}").classes("tp-link text-caption")
                        else:
                            ui.label(f"{a['requirement']}: {a['attach']}").classes("text-caption " + ("text-negative" if a["status"] == "missing" else ""))
            if status == "interrupted" and intr and pc.can(Perm.BIDS_WRITE):
                with ui.row().classes("items-center gap-2 q-mt-sm"):
                    ui.label(intr.get("question", "Decision needed")).classes("text-weight-medium")

                    def decide(d):
                        if run(lambda db: bid_preparation.resume(db, pc.ctx, rid, d), success="Decision recorded"):
                            ui.navigate.reload() if d == "submit" else (panel.refresh(), on_drafts_changed())
                    ui.button("Submit for internal review", on_click=lambda: decide("submit")).props("unelevated color=secondary")
                    ui.button("Keep as draft", on_click=lambda: decide("keep_draft")).props("outline color=primary")
            if status == "running":
                ui.timer(2.0, lambda: (panel.refresh(), on_drafts_changed()), once=True)
    panel()


def _drafts(pc, wid, ws_status):
    @ui.refreshable
    def body():
        with session_scope() as db:
            drafts = [(d.id, d.kind, d.title, d.version_no, d.status, d.sections, d.generator, d.lock_version) for d in bids.drafts(db, pc.ctx, wid)]
        if pc.can(Perm.BIDS_WRITE):
            ui.button("Generate drafts from verified evidence", icon="auto_fix_high",
                      on_click=lambda: run(lambda db: generation.generate_drafts(db, pc.ctx, wid), success="Drafts generated", refresh=body.refresh)).props("unelevated color=secondary")
        ui.label("Every statement is drawn from your records with a [Source: …] citation. Unknown facts appear as [TO BE COMPLETED: …] — nothing is invented.").classes("text-caption tp-muted")
        if not drafts:
            ui.label("No drafts yet.").classes("tp-muted")
        for did, kind, title, vno, status, sections, gen, lv in drafts:
            with ui.card().classes("tp-card w-full q-pa-md"):
                with ui.row().classes("w-full items-center justify-between"):
                    ui.label(f"{title} · v{vno}").classes("tp-h2")
                    with ui.row().classes("items-center gap-2"):
                        ui.badge(status.replace("_", " "), color=BID_STATUS_COLOR.get(status, "grey")).props("outline")
                        ui.badge(gen, color="grey-7").props("outline")
                editors = []
                for s in sections:
                    ui.label(s["heading"] + ("  · AI-written, claims checked" if s.get("ai_generated") else "")).classes("text-subtitle2 q-mt-sm")
                    ta = ui.textarea(value=s["body"]).props("outlined autogrow").classes("w-full")
                    ta.set_enabled(pc.can(Perm.BIDS_WRITE) and ws_status in ("draft", "changes_requested"))
                    editors.append((s, ta))
                if pc.can(Perm.BIDS_WRITE) and ws_status in ("draft", "changes_requested"):
                    ui.button("Save as new version", icon="save", on_click=lambda did=did, editors=editors, lv=lv: run(
                        lambda db: bids.edit_draft(db, pc.ctx, did, [{**s, "body": ta.value} for s, ta in editors], lv), success="Saved new version", refresh=body.refresh)).props("dense unelevated color=primary")
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
        with ui.row().classes("w-full gap-2"):
            for k, v in summary.items():
                ui.badge(f"{k.replace('_', ' ')}: {v}", color="grey-7").props("outline")
        if issues:
            with ui.expansion(f"{len(issues)} issue(s) detected", icon="warning").classes("tp-card w-full"):
                for i in issues:
                    ui.label(f"{i['type'].replace('_', ' ')} — rows {', '.join(map(str, i['rows']))}: {i['description']}" + (f" (units: {', '.join(i['units'])})" if i.get("units") else "")).classes("text-body2")
        ui.label("Internal estimate — kept separate from the prescribed BOQ. Enter rates below; amounts use exact decimal arithmetic.").classes("text-caption tp-muted")
        rate_inputs = {}
        with ui.element("div").classes("w-full tp-card").style("max-height: 520px; overflow: auto"):
            with ui.grid(columns="60px 70px 1fr 70px 90px 130px").classes("w-full q-pa-sm gap-1 items-center"):
                for h in ("Row", "Item", "Description", "Unit", "Qty", "Rate (₹)"):
                    ui.label(h).classes("text-caption text-weight-bold tp-muted")
                for iid_, row, code, desc, unit, qty, rate, locked, heading, sub, its, formula in items:
                    ui.label(str(row)).classes("text-caption")
                    ui.label(code or "").classes("text-caption")
                    ui.label(desc).classes("text-body2 " + ("text-weight-bold" if heading or sub else "")).tooltip(", ".join(its) if its else (formula or ""))
                    ui.label(unit or ("—" if not (heading or sub) else "")).classes("text-caption" + (" text-negative" if "missing_unit" in its else ""))
                    ui.label(str(qty) if qty is not None else ("—" if not (heading or sub) else "")).classes("text-caption" + (" text-negative" if "missing_quantity" in its else ""))
                    if heading or sub:
                        ui.label("")
                    else:
                        rate_inputs[iid_] = ui.input(value=str(rate) if rate is not None else "").props("dense outlined" + (" readonly" if locked else "")).classes("w-full")
        with ui.row().classes("w-full items-center gap-2"):
            name = ui.input("Scenario name", value="Base estimate").props("outlined dense")
            mk = ui.input("Markup %", value="10").props("outlined dense").style("width:110px")
            gst = ui.input("GST %", value="18").props("outlined dense").style("width:110px")
            ui.button("Calculate & save scenario", on_click=lambda: run(lambda db: boq.save_scenario(db, pc.ctx, iid, name.value, {k: v.value for k, v in rate_inputs.items() if v.value}, mk.value, gst.value),
                                                                        success="Scenario saved", refresh=body.refresh)).props("unelevated color=primary")
        for sid, sname, totals in scen:
            with ui.card().classes("tp-card w-full q-pa-sm"):
                with ui.row().classes("w-full items-center justify-between"):
                    ui.label(f"{sname}: subtotal {format_inr(Decimal(totals['subtotal']))} + GST {format_inr(Decimal(totals['gst']))} = {format_inr(Decimal(totals['grand_total']))}").classes("text-weight-medium")
                    ui.label(f"{totals['priced_items']} priced · {totals['unpriced_items']} unpriced{' · INCOMPLETE' if not totals['complete'] else ''}").classes("text-caption " + ("text-warning" if not totals["complete"] else "tp-muted"))

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
        if pc.can(Perm.BIDS_WRITE):
            ui.button("Export submission package", icon="inventory_2", on_click=lambda: run(lambda db: generation.export_package(db, pc.ctx, wid),
                                                                                           success="Package exported", refresh=body.refresh)).props("unelevated color=secondary")
        ui.label("Generates the technical response (DOCX), capability summary, compliance matrix and checklist (XLSX), evidence and risk reports (PDF), "
                 "and a ZIP with the exact supporting document versions plus a SHA-256 manifest.").classes("text-caption tp-muted")
        if not arts:
            ui.label("No exports yet.").classes("tp-muted")
        for kind, fmt, title, vid, at in arts:
            with ui.row().classes("w-full items-center justify-between no-wrap q-py-xs").style("border-bottom:1px solid #EEF1F5"):
                with ui.row().classes("items-center gap-2"):
                    ui.badge(fmt.upper(), color="primary").props("outline")
                    ui.label(title).classes("text-body2")
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
        ui.label("Team").classes("tp-h1")

        @ui.refreshable
        def body():
            with session_scope() as db:
                ms = [(m.id, u.full_name, u.email, m.role, m.is_active) for m, u in org_service.list_members(db, pc.ctx)]
                invs = [(i.id, i.email, i.role, i.expires_at) for i in org_service.list_invitations(db, pc.ctx)] if pc.can(Perm.MEMBERS_MANAGE) else []
            with ui.card().classes("tp-card w-full q-pa-md"):
                ui.label("Members").classes("tp-h2")
                for mid, nm, email, role, active in ms:
                    with ui.row().classes("w-full items-center justify-between no-wrap q-py-xs"):
                        ui.label(f"{nm} · {email}" + ("" if active else " (removed)")).classes("" if active else "tp-muted")
                        if pc.can(Perm.MEMBERS_MANAGE) and active:
                            with ui.row().classes("items-center gap-1"):
                                ui.select({r.value: ROLE_LABELS[r] for r in Role}, value=role,
                                          on_change=lambda e, mid=mid: run(lambda db: org_service.change_member_role(db, pc.ctx, mid, e.value), success="Role updated", refresh=body.refresh)).props("dense outlined").style("width:200px")
                                ui.button(icon="person_remove", on_click=lambda mid=mid: run(lambda db: org_service.remove_member(db, pc.ctx, mid), success="Member removed", refresh=body.refresh)).props("flat dense color=negative")
                        else:
                            ui.badge(ROLE_LABELS.get(role, role), color="grey-7").props("outline")
            if pc.can(Perm.MEMBERS_MANAGE):
                with ui.card().classes("tp-card w-full q-pa-md"):
                    ui.label("Invite a teammate").classes("tp-h2")
                    with ui.row().classes("w-full gap-2"):
                        email = ui.input("Email").props("outlined dense").classes("col")
                        role = ui.select({r.value: ROLE_LABELS[r] for r in Role if r != Role.OWNER or pc.ctx.role == Role.OWNER}, value="contributor").props("outlined dense").style("width:220px")

                        def invite():
                            res = run(lambda db: org_service.invite_member(db, pc.ctx, email.value or "", role.value), success="Invitation sent", refresh=body.refresh)
                            if res and get_settings().environment == "development":
                                ui.notify(f"Dev invite link: {get_settings().base_url}/invite?token={res[1]}", type="info", timeout=0, close_button=True, multi_line=True)
                        ui.button("Invite", on_click=invite).props("unelevated color=primary")
                    for iid, em, rl, exp in invs:
                        with ui.row().classes("w-full items-center justify-between"):
                            ui.label(f"{em} — {ROLE_LABELS.get(rl, rl)} · expires {exp.astimezone(IST):%d %b}").classes("text-body2")
                            ui.button("Revoke", on_click=lambda iid=iid: run(lambda db: org_service.revoke_invitation(db, pc.ctx, iid), success="Revoked", refresh=body.refresh)).props("flat dense color=negative")
        body()


@ui.page("/settings")
def settings_page(request: Request):
    with shell(request, "/settings") as pc:
        if not pc:
            return
        ui.label("Settings").classes("tp-h1")
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
        with ui.row().classes("w-full gap-4 items-start"):
            with ui.column().classes("col gap-4"):
                with ui.card().classes("tp-card w-full q-pa-md"):
                    ui.label("Organization").classes("tp-h2")
                    nm = ui.input("Name", value=name).props("outlined dense").classes("w-full")
                    lh = ui.input("Letterhead name on generated documents", value=osettings.get("letterhead_name", "")).props("outlined dense").classes("w-full")
                    pol = ui.select({"reopen_approvals_on_mandatory_change": "Reopen approvals when a corrigendum changes a mandatory condition",
                                     "flag_only": "Only flag affected items"}, value=osettings.get("amendment_policy", "reopen_approvals_on_mandatory_change"), label="Corrigendum policy").props("outlined dense").classes("w-full")
                    if pc.can(Perm.ORG_MANAGE):
                        ui.button("Save", on_click=lambda: run(lambda db: org_service.update_org_settings(db, pc.ctx, name=nm.value, settings={"letterhead_name": lh.value, "amendment_policy": pol.value}), success="Saved")).props("unelevated color=primary")
                _template_card(pc, osettings)
                with ui.card().classes("tp-card w-full q-pa-md"):
                    ui.label("Notifications").classes("tp-h2")
                    for kind, label in [("deadline", "Deadline reminders"), ("document_expiry", "Document expiry warnings"), ("task_assigned", "Task assignments"),
                                        ("review_requested", "Review requests"), ("approval", "Approvals"), ("amendment", "Tender amendments"), ("job_failed", "Failed processing")]:
                        with ui.row().classes("w-full items-center justify-between"):
                            ui.label(label)
                            em = ui.switch("Email", value=True)
                            em.on_value_change(lambda e, kind=kind: run(lambda db: notifications.set_preference(db, pc.ctx, kind, True, e.value), success="Preference saved"))
                    ui.label("Email delivery status is recorded only as the provider reports it; without SMTP configured, emails are not sent." if not get_settings().smtp_host else "SMTP provider configured.").classes("text-caption tp-muted")
                with ui.card().classes("tp-card w-full q-pa-md"):
                    ui.label("API tokens (MCP & integrations)").classes("tp-h2")
                    ui.label("Tokens act as you within this organization. Add the mcp:write scope only if an external agent may change data.").classes("text-caption tp-muted")
                    for tid_, tname, prefix, scopes, revoked, exp in tokens:
                        with ui.row().classes("w-full items-center justify-between"):
                            ui.label(f"{tname} · {prefix}… · {', '.join(scopes) or 'role permissions'}" + (" · revoked" if revoked else "")).classes("text-body2")
                            if not revoked:
                                ui.button("Revoke", on_click=lambda tid_=tid_: (run(lambda db: auth_service.revoke_api_token(db, pc.ctx, tid_), success="Revoked"), ui.navigate.reload())).props("flat dense color=negative")
                    with ui.row().classes("w-full gap-2 items-center"):
                        tn = ui.input("Token name").props("outlined dense").classes("col")
                        wr = ui.checkbox("mcp:write")

                        def mk():
                            res = run(lambda db: auth_service.create_api_token(db, pc.ctx, tn.value or "Token", ["mcp:write", "*"] if wr.value else []))
                            if res:
                                with ui.dialog() as dlg, ui.card():
                                    ui.label("Copy your token now — it won't be shown again.").classes("tp-h2")
                                    ui.code(res[1]).classes("w-full")
                                    ui.label(f"MCP endpoint: {get_settings().base_url}/mcp/  (Authorization: Bearer <token>)").classes("text-caption")
                                    ui.button("Done", on_click=lambda: (dlg.close(), ui.navigate.reload())).props("flat")
                                dlg.open()
                        if pc.can(Perm.API_TOKENS):
                            ui.button("Create token", on_click=mk).props("unelevated color=primary")
            with ui.column().classes("col gap-4"):
                with ui.card().classes("tp-card w-full q-pa-md"):
                    ui.label("Plan & usage").classes("tp-h2")
                    ui.label(f"{plan_name or '—'} · {sub_status or ''}" + (f" · period ends {period_end.astimezone(IST):%d %b %Y}" if period_end else "")).classes("text-weight-medium")
                    if pending:
                        ui.label(f"Requested change to {pending} — pending administrator (no payment processor configured).").classes("text-caption text-warning")
                    for metric, used in usage.items():
                        lim = limits.get(f"{metric}_per_month")
                        ui.label(f"{metric.replace('_', ' ')}: {used:,} / {lim:,} this month" if lim else f"{metric}: {used}").classes("text-body2")
                        if lim:
                            ui.linear_progress(value=min(1, used / lim), show_value=False).props("rounded")
                    ui.label(f"Storage: {storage / 1024**2:.1f} MB / {limits.get('storage_bytes', 0) / 1024**3:.0f} GB").classes("text-body2")
                    if pc.can(Perm.ORG_BILLING):
                        with ui.row().classes("gap-2 q-mt-sm"):
                            for code in ("starter", "professional", "enterprise"):
                                ui.button(f"Request {code}", on_click=lambda code=code: run(lambda db: entitlements.request_plan_change(db, pc.ctx.organization_id, code, pc.ctx.user_id), success="Request recorded")).props("dense outline")
                with ui.card().classes("tp-card w-full q-pa-md"):
                    ui.label("Integrations").classes("tp-h2")
                    for key, c in CONNECTORS.items():
                        with ui.row().classes("w-full items-center justify-between"):
                            with ui.column().classes("gap-0 col"):
                                ui.label(c.name).classes("text-weight-medium")
                                ui.label(c.description).classes("text-caption tp-muted")
                            ui.badge("available" if c.available() else "not configured", color="positive" if c.available() else "grey-6").props("outline")
                if pc.can(Perm.ORG_DELETE):
                    with ui.card().classes("tp-card w-full q-pa-md").style("border-color:#F1C4BE"):
                        ui.label("Danger zone").classes("tp-h2 text-negative")
                        ui.label("Request deletion of this organization. Data and stored files are purged after a 30-day grace period.").classes("text-caption tp-muted")
                        ui.button("Request deletion", on_click=lambda: run(lambda db: org_service.request_deletion(db, pc.ctx), success="Deletion requested")).props("outline color=negative")


_ = (Decimal, format_inr_short)


def _template_card(pc, osettings):
    """Branding + organization-authored standard sections used in generated bid documents."""
    b = osettings.get("branding") or {}
    secs = list(((osettings.get("template") or {}).get("standard_sections")) or [])
    editable = pc.can(Perm.ORG_MANAGE)
    with ui.card().classes("tp-card w-full q-pa-md"):
        ui.label("Document template & branding").classes("tp-h2")
        ui.label("Applied to generated DOCX, XLSX and PDF files. Standard sections are your own approved text (e.g. quality policy, "
                 "after-sales support) and are inserted verbatim into technical responses, labelled as organization template text.").classes("text-caption tp-muted")
        lh = ui.input("Letterhead name", value=b.get("letterhead_name") or osettings.get("letterhead_name", "")).props("outlined dense").classes("w-full")
        addr = ui.input("Address line", value=b.get("address_line", "")).props("outlined dense").classes("w-full")
        foot = ui.input("Footer text", value=b.get("footer_text", "")).props("outlined dense").classes("w-full")
        color = ui.color_input("Brand colour", value=b.get("primary_color", "#1F2A44")).props("outlined dense").classes("w-full")
        editors = []
        for i in range(3):
            cur = secs[i] if i < len(secs) else {}
            with ui.expansion(f"Standard section {i + 1}" + (f": {cur['heading']}" if cur.get("heading") else "")).classes("w-full"):
                h = ui.input("Heading", value=cur.get("heading", "")).props("outlined dense").classes("w-full")
                body = ui.textarea("Text", value=cur.get("body", "")).props("outlined autogrow").classes("w-full")
                editors.append((h, body))
        for el in [lh, addr, foot, color] + [e for pair in editors for e in pair]:
            el.set_enabled(editable)
        if editable:
            ui.button("Save template", on_click=lambda: run(lambda db: org_service.update_document_template(
                db, pc.ctx, branding_in={"letterhead_name": lh.value, "address_line": addr.value, "footer_text": foot.value, "primary_color": color.value},
                standard_sections=[{"heading": h.value, "body": bd.value} for h, bd in editors]), success="Template saved")).props("unelevated color=primary")


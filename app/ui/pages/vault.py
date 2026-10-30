"""Document vault, document detail/versions, company profile & facts, copilot chat."""

from __future__ import annotations

from fastapi import Request
from nicegui import ui
from sqlalchemy import select

from app.ai.agents import copilot
from app.auth.deps import parse_uuid
from app.auth.permissions import Perm
from app.core.errors import AppError
from app.db.models import (
    CompanyCertification,
    CompanyFinancialRecord,
    CompanyProfile,
    CompanyProject,
    CompanyRegistration,
    DocumentCategory,
    FactStatus,
    Tender,
)
from app.db.session import session_scope
from app.extraction.normalization.values import IST, format_inr
from app.mcp.tools.registry import call_tool
from app.services import audit
from app.services.documents import service as documents
from app.ui.components.common import download_button, upload_box
from app.ui.layouts.shell import run, shell
from app.ui.themes.theme import empty_state

CATS = {c.value: c.value.replace("_", " ").capitalize() for c in DocumentCategory}


@ui.page("/vault")
def vault(request: Request):
    with shell(request, "/vault") as pc:
        if not pc:
            return
        ui.label("Document Vault").classes("tp-h1")
        ui.label("Your qualification documents. Each file is versioned; assessments always reference the exact version they used.").classes("tp-muted")
        flt = {"cat": None, "q": ""}

        @ui.refreshable
        def grid():
            with session_scope() as db:
                rows, total = documents.list_documents(db, pc.ctx, scope="company", category=flt["cat"], q=flt["q"] or None, limit=200)
                data = [(d.id, d.title, d.category, d.category_confidence, d.category_confirmed, v.version_no if v else None, v.processing_status if v else "—",
                         v.expires_on if v else None, v.needs_review if v else False, d.is_synthetic) for d, v in rows]
            if not data:
                empty_state("folder_open", "No documents yet", "Upload certificates, financial statements, completion certificates and catalogues.")
                return
            with ui.grid(columns=3).classes("w-full gap-3"):
                for did, title, cat, conf, confirmed, vno, status, exp, review, synth in data:
                    with ui.card().classes("tp-card q-pa-md cursor-pointer").on("click", lambda did=did: ui.navigate.to(f"/vault/{did}")):
                        with ui.row().classes("w-full justify-between no-wrap"):
                            ui.label(title).classes("text-weight-medium ellipsis")
                            ui.badge(f"v{vno}", color="grey-7").props("outline")
                        ui.label(CATS.get(cat, cat) + ("" if confirmed else f" · auto {conf:.0%}")).classes("text-caption tp-muted")
                        with ui.row().classes("gap-1 q-mt-xs"):
                            ui.badge(status, color="positive" if status == "ready" else "warning" if status in ("needs_review", "processing", "pending") else "negative").props("outline")
                            if review:
                                ui.badge("confirm fields", color="info")
                            if exp:
                                from datetime import date

                                ui.badge(("expired " if exp < date.today() else "valid to ") + exp.strftime("%d %b %Y"), color="negative" if exp < date.today() else "grey-7").props("outline")
                            if synth:
                                ui.badge("synthetic", color="orange").props("outline")

        with ui.row().classes("w-full gap-2"):
            ui.select({None: "All categories", **CATS}, value=None, on_change=lambda e: (flt.update(cat=e.value), grid.refresh())).props("outlined dense").style("width:260px")
            s = ui.input(placeholder="Search titles").props("outlined dense clearable").classes("col")
            s.on("update:model-value", lambda e: (flt.update(q=s.value or ""), grid.refresh()), throttle=0.4)
        if pc.can(Perm.DOCUMENTS_UPLOAD):
            upload_box(pc, scope="company", on_done=grid.refresh)
        grid()


@ui.page("/vault/{document_id}")
def vault_document(request: Request, document_id: str):
    did = parse_uuid(document_id)
    with shell(request, "/vault") as pc:
        if not pc:
            return

        @ui.refreshable
        def body():
            try:
                with session_scope() as db:
                    d = documents.get_document(db, pc.ctx, did)
                    vers = [(v.id, v.version_no, v.original_filename, v.processing_status, v.created_at, v.sha256, v.expires_on, v.issued_on, v.page_count,
                             v.extraction_method, v.quality_notes, v.language) for v in documents.versions(db, pc.ctx, did)]
                    head = (d.title, d.category, d.category_confidence, d.category_confirmed, d.current_version_id)
                    fields = [(f.id, f.name, f.corrected_value or f.value, f.confidence, f.needs_confirmation, f.confirmed_at)
                              for f in (documents.fields(db, pc.ctx, d.current_version_id) if d.current_version_id else [])]
                    facts = _facts_for_doc(db, pc.ctx.organization_id, did)
            except AppError as exc:
                ui.label(exc.message).classes("text-negative")
                return
            title, cat, conf, confirmed, cur = head
            ui.link("← Document vault", "/vault").classes("tp-link text-caption")
            with ui.row().classes("w-full items-center justify-between"):
                ui.label(title).classes("tp-h1")
                with ui.row():
                    if cur:
                        ui.button("View", icon="visibility", on_click=lambda: ui.navigate.to(f"/viewer/{cur}")).props("outline color=primary")
                        download_button(pc, cur, props="outline color=primary")
                    if pc.can(Perm.DOCUMENTS_MANAGE):
                        ui.button("Delete", icon="delete", on_click=lambda: confirm_delete()).props("flat color=negative")
            with ui.row().classes("w-full gap-4 items-start"):
                with ui.card().classes("tp-card col q-pa-md"):
                    ui.label("Classification").classes("tp-h2")
                    ui.label(f"{'Confirmed' if confirmed else f'Detected automatically ({conf:.0%} confidence)'}").classes("text-caption tp-muted")
                    sel = ui.select(CATS, value=cat, label="Category").props("outlined dense").classes("w-full")
                    if pc.can(Perm.DOCUMENTS_UPLOAD):
                        ui.button("Confirm category", on_click=lambda: run(lambda db: documents.confirm_category(db, pc.ctx, did, sel.value), success="Category confirmed", refresh=body.refresh)).props("dense unelevated color=primary q-mt-sm")
                    ui.label("Extracted fields").classes("tp-h2 q-mt-md")
                    if not fields:
                        ui.label("No metadata fields extracted.").classes("tp-muted")
                    for fid, name, value, fconf, needs, confirmed_at in fields:
                        with ui.row().classes("w-full items-center justify-between no-wrap"):
                            ui.label(name.replace("_", " ")).classes("tp-muted")
                            inp = ui.input(value=value).props("dense outlined").style("width:220px")
                            if confirmed_at:
                                ui.icon("verified", color="positive").tooltip("Confirmed by a user")
                            elif pc.can(Perm.DOCUMENTS_UPLOAD):
                                ui.button(icon="check", on_click=lambda fid=fid, inp=inp: run(lambda db: documents.confirm_field(db, pc.ctx, fid, inp.value), success="Field confirmed", refresh=body.refresh)).props("dense flat color=positive").tooltip(f"Confirm ({fconf:.0%} extraction confidence)")
                with ui.card().classes("tp-card col q-pa-md"):
                    ui.label("Facts available to eligibility checks").classes("tp-h2")
                    ui.label("Document-stated values. Confirm them to raise confidence; reject incorrect extractions.").classes("text-caption tp-muted")
                    if not facts:
                        ui.label("No structured facts extracted from this document.").classes("tp-muted")
                    for kind, fid, desc, page, status, fconf in facts:
                        with ui.row().classes("w-full items-center justify-between no-wrap q-py-xs"):
                            with ui.column().classes("gap-0 col"):
                                ui.label(desc).classes("text-body2")
                                ui.label(f"{kind} · p.{page} · {status} · {fconf:.0%}").classes("text-caption tp-muted")
                            if pc.can(Perm.DOCUMENTS_UPLOAD) and status == "extracted":
                                ui.button(icon="check", on_click=lambda kind=kind, fid=fid: run(lambda db: _set_fact(db, pc, kind, fid, FactStatus.CONFIRMED), success="Confirmed", refresh=body.refresh)).props("dense flat color=positive")
                                ui.button(icon="close", on_click=lambda kind=kind, fid=fid: run(lambda db: _set_fact(db, pc, kind, fid, FactStatus.REJECTED), success="Rejected", refresh=body.refresh)).props("dense flat color=negative")
            with ui.card().classes("tp-card w-full q-pa-md"):
                ui.label("Versions").classes("tp-h2")
                for vid, vno, fname, status, at, sha, exp, iss, pages, method, notes, lang in vers:
                    with ui.row().classes("w-full items-center justify-between no-wrap q-py-xs").style("border-bottom:1px solid #EEF1F5"):
                        ui.label(f"v{vno} · {fname}").classes("text-weight-medium" if vid == cur else "")
                        ui.label(f"{at.astimezone(IST):%d %b %Y %H:%M} · {status} · {pages or '?'} p · {method or ''} · {lang or ''} · sha256 {sha[:10]}…").classes("text-caption tp-muted")
                        with ui.row():
                            ui.button(icon="visibility", on_click=lambda vid=vid: ui.navigate.to(f"/viewer/{vid}")).props("flat dense")
                            download_button(pc, vid, label="")
                if pc.can(Perm.DOCUMENTS_UPLOAD):
                    ui.label("Upload a replacement (new version). Earlier assessments keep referencing the version they used.").classes("text-caption tp-muted q-mt-sm")
                    upload_box(pc, document_id=did, on_done=body.refresh, label="Upload new version")

        def confirm_delete():
            with ui.dialog() as dlg, ui.card():
                ui.label("Delete this document from the vault?").classes("tp-h2")
                ui.label("It will no longer be used as evidence. Historical assessments keep their snapshot.").classes("tp-muted")
                with ui.row():
                    ui.button("Delete", on_click=lambda: (run(lambda db: documents.soft_delete(db, pc.ctx, did), success="Deleted"), dlg.close(), ui.navigate.to("/vault"))).props("unelevated color=negative")
                    ui.button("Cancel", on_click=dlg.close).props("flat")
            dlg.open()

        body()


FACT_MODELS = {"certification": CompanyCertification, "financial": CompanyFinancialRecord, "project": CompanyProject, "registration": CompanyRegistration}


def _facts_for_doc(db, org_id, did):
    out = []
    for r in db.scalars(select(CompanyCertification).where(CompanyCertification.organization_id == org_id, CompanyCertification.document_id == did)):
        out.append(("certification", r.id, f"{r.cert_type} {r.certificate_number or ''} — valid {r.valid_from or '?'} → {r.valid_until or '?'}", r.source_page, r.status, r.extraction_confidence))
    for r in db.scalars(select(CompanyFinancialRecord).where(CompanyFinancialRecord.organization_id == org_id, CompanyFinancialRecord.document_id == did)):
        out.append(("financial", r.id, f"FY {r.fiscal_year} {r.metric}: {format_inr(r.amount)}{' (audited)' if r.audited else ''}", r.source_page, r.status, r.extraction_confidence))
    for r in db.scalars(select(CompanyProject).where(CompanyProject.organization_id == org_id, CompanyProject.document_id == did)):
        out.append(("project", r.id, f"{r.title[:90]} — {format_inr(r.contract_value) if r.contract_value else 'value n/a'}, completed {r.completion_date or 'n/a'}", r.source_page, r.status, r.extraction_confidence))
    for r in db.scalars(select(CompanyRegistration).where(CompanyRegistration.organization_id == org_id, CompanyRegistration.document_id == did)):
        out.append(("registration", r.id, f"{r.kind.upper()}: {r.identifier}", r.source_page, r.status, r.extraction_confidence))
    return out


def _set_fact(db, pc, kind, fid, status):
    pc.ctx.require(Perm.DOCUMENTS_UPLOAD)
    model = FACT_MODELS[kind]
    row = db.scalar(select(model).where(model.id == fid, model.organization_id == pc.ctx.organization_id))
    if not row:
        raise AppError("Fact not found.")
    from app.db.base import utcnow

    row.status, row.confirmed_by_id, row.confirmed_at = status, pc.ctx.user_id, utcnow()
    audit.record(db, f"fact.{status}", ctx=pc.ctx, subject_type=kind, subject_id=fid)


@ui.page("/company")
def company(request: Request):
    with shell(request, "/company") as pc:
        if not pc:
            return
        with session_scope() as db:
            p = db.scalar(select(CompanyProfile).where(CompanyProfile.organization_id == pc.ctx.organization_id))
            vals = {c.name: getattr(p, c.name) for c in CompanyProfile.__table__.columns}
            certs = [(c.cert_type, c.certificate_number, c.valid_until, c.status) for c in db.scalars(select(CompanyCertification).where(
                CompanyCertification.organization_id == pc.ctx.organization_id, CompanyCertification.status != FactStatus.SUPERSEDED))]
            fins = [(f.fiscal_year, f.amount, f.audited, f.status) for f in db.scalars(select(CompanyFinancialRecord).where(
                CompanyFinancialRecord.organization_id == pc.ctx.organization_id, CompanyFinancialRecord.status != FactStatus.SUPERSEDED).order_by(CompanyFinancialRecord.fiscal_year))]
            projects = [(x.title, x.contract_value, x.completion_date) for x in db.scalars(select(CompanyProject).where(
                CompanyProject.organization_id == pc.ctx.organization_id, CompanyProject.status != FactStatus.SUPERSEDED))]
        ui.label("Company Profile").classes("tp-h1")
        with ui.row().classes("w-full gap-4 items-start"):
            with ui.card().classes("tp-card col q-pa-md"):
                ui.label("Profile").classes("tp-h2")
                f = {}
                for key, label in [("legal_name", "Legal name"), ("industry", "Industry"), ("business_category", "Business category"), ("gstin", "GSTIN"), ("pan", "PAN"),
                                   ("cin", "CIN"), ("udyam_number", "Udyam"), ("msme_category", "MSME category"), ("state", "State"), ("address", "Address")]:
                    f[key] = ui.input(label, value=vals.get(key)).props("outlined dense").classes("w-full")
                f["year_established"] = ui.number("Year established", value=vals.get("year_established"), format="%d").props("outlined dense").classes("w-full")
                for key, label in [("operational_states", "Operational states"), ("capabilities", "Capabilities"), ("product_categories", "Product categories"), ("tender_interests", "Tender interests")]:
                    f[key] = ui.input_chips(label, value=vals.get(key) or []).props("outlined dense").classes("w-full")

                def save():
                    def do(db):
                        pc.ctx.require(Perm.COMPANY_WRITE)
                        prof = db.scalar(select(CompanyProfile).where(CompanyProfile.organization_id == pc.ctx.organization_id))
                        for k, el in f.items():
                            v = el.value
                            if k == "year_established":
                                v = int(v) if v else None
                            setattr(prof, k, v or ([] if k in ("operational_states", "capabilities", "product_categories", "tender_interests") else None))
                        audit.record(db, "company.profile_updated", ctx=pc.ctx, subject_type="company_profile", subject_id=prof.id)
                    run(do, success="Profile saved")
                if pc.can(Perm.COMPANY_WRITE):
                    ui.button("Save profile", on_click=save).props("unelevated color=primary")
                ui.label("Identifiers are document-stated or user-entered and not verified with government registries.").classes("text-caption tp-muted")
            with ui.column().classes("col gap-4"):
                with ui.card().classes("tp-card w-full q-pa-md"):
                    ui.label("Certifications on file").classes("tp-h2")
                    from datetime import date

                    for t, n, vu, st in certs:
                        ui.label(f"{t} {n or ''} — {'expired' if vu and vu < date.today() else 'valid to'} {vu or '?'} ({st})").classes("text-body2 " + ("text-negative" if vu and vu < date.today() else ""))
                    if not certs:
                        ui.label("None extracted yet.").classes("tp-muted")
                with ui.card().classes("tp-card w-full q-pa-md"):
                    ui.label("Turnover records").classes("tp-h2")
                    for fy, amt, aud, st in fins:
                        ui.label(f"FY {fy}: {format_inr(amt)} {'(audited)' if aud else ''} — {st}").classes("text-body2")
                    if not fins:
                        ui.label("None extracted yet.").classes("tp-muted")
                with ui.card().classes("tp-card w-full q-pa-md"):
                    ui.label("Completed projects").classes("tp-h2")
                    for title, v, done in projects:
                        ui.label(f"{title[:80]} — {format_inr(v) if v else 'value n/a'}, {done or 'date n/a'}").classes("text-body2")
                    if not projects:
                        ui.label("None extracted yet.").classes("tp-muted")


@ui.page("/copilot")
def copilot_page(request: Request):
    with shell(request, "/copilot") as pc:
        if not pc:
            return
        tender_q = request.query_params.get("tender")
        conv_q = request.query_params.get("c")
        with session_scope() as db:
            convs = [(c.id, c.title, c.tender_id) for c in copilot.list_conversations(db, pc.ctx)]
            tenders_opts = {str(t.id): t.title[:70] for t in db.scalars(select(Tender).where(Tender.organization_id == pc.ctx.organization_id, Tender.deleted_at.is_(None)))}
        state = {"cid": parse_uuid(conv_q) if conv_q else None}
        with ui.row().classes("w-full gap-4 no-wrap items-stretch").style("height: calc(100vh - 140px)"):
            with ui.card().classes("tp-card q-pa-sm").style("width: 260px"):
                ui.label("Conversations").classes("tp-h2 q-pa-sm")
                tender_sel = ui.select({"": "No tender context", **tenders_opts}, value=tender_q or "", label="Tender context").props("outlined dense").classes("w-full")

                def new_conv():
                    from uuid import UUID

                    cid = run(lambda db: copilot.start_conversation(db, pc.ctx, UUID(tender_sel.value) if tender_sel.value else None).id)
                    if cid:
                        ui.navigate.to(f"/copilot?c={cid}")
                ui.button("New conversation", icon="add", on_click=new_conv).props("unelevated color=secondary").classes("w-full")
                with ui.scroll_area().classes("col"):
                    for cid, title, tid in convs:
                        ui.item(title[:40], on_click=lambda cid=cid: ui.navigate.to(f"/copilot?c={cid}")).classes("rounded-borders" + (" bg-grey-2" if cid == state["cid"] else ""))
            with ui.card().classes("tp-card col q-pa-md column no-wrap"):
                if not state["cid"]:
                    empty_state("auto_awesome", "Procurement Copilot", "Ask about eligibility, evidence, deadlines or missing documents. Answers are grounded in your "
                                "own records via TenderPilot tools, with sources. Pick a tender context and start a conversation.")
                    return
                msgs_area = ui.scroll_area().classes("col w-full")
                status_line = ui.label("").classes("text-caption tp-muted")

                def render():
                    msgs_area.clear()
                    try:
                        with session_scope() as db:
                            msgs = [(m.id, m.role, m.content, m.citations, m.tool_calls, m.suggestions, m.mode) for m in copilot.messages(db, pc.ctx, state["cid"])]
                    except AppError as exc:
                        with msgs_area:
                            ui.label(exc.message).classes("text-negative")
                        return
                    with msgs_area:
                        for mid, role, content, cites, calls, sugg, mode in msgs:
                            with ui.chat_message(sent=role == "user", name="You" if role == "user" else "Copilot").classes("w-full"):
                                ui.markdown(content).classes("text-body2")
                                if role == "assistant":
                                    for c in cites:
                                        if c.get("document_version_id"):
                                            ui.link(f"📄 {c['document']} p.{c.get('page')}", f"/viewer/{c['document_version_id']}?page={c.get('page') or 1}").classes("tp-link text-caption")
                                        else:
                                            ui.label(f"📄 {c['document']} p.{c.get('page')}").classes("text-caption tp-muted")
                                    done = [c for c in calls if c.get("status") == "done"]
                                    if done:
                                        ui.label("Tools used: " + ", ".join(c["tool"] for c in done)).classes("text-caption tp-muted")
                                    for i, c in enumerate(calls):
                                        if c.get("status") == "pending_approval":
                                            with ui.card().classes("q-pa-sm").props("flat bordered"):
                                                ui.label(f"Proposed action: {c['tool'].replace('_', ' ')}").classes("text-weight-medium")
                                                ui.label("Requires your approval before anything is changed.").classes("text-caption tp-muted")
                                                ui.button("Approve & run", on_click=lambda mid=mid, i=i: run(lambda db: copilot.execute_pending_action(db, pc.ctx, mid, i), success="Action completed", refresh=render)).props("dense unelevated color=secondary")
                                        elif c.get("status") == "approved_executed":
                                            ui.label(f"✓ {c['tool'].replace('_', ' ')} approved and executed").classes("text-caption text-positive")
                                    with ui.row().classes("gap-1"):
                                        for s_ in sugg[:3]:
                                            ui.button(s_, on_click=lambda s_=s_: send(s_)).props("dense flat no-caps color=secondary").classes("text-caption")
                                        if pc.can(Perm.TASKS_WRITE):
                                            ui.button("Save as note", icon="note_add", on_click=lambda content=content: save_note(content)).props("dense flat no-caps")
                    msgs_area.scroll_to(percent=1.0)

                def save_note(content):
                    with session_scope() as db:
                        conv = copilot.get_conversation(db, pc.ctx, state["cid"])
                        tid = conv.tender_id
                    if not tid:
                        ui.notify("Attach a tender to this conversation to save notes into its workspace.", type="warning")
                        return
                    run(lambda db: call_tool(db, pc.ctx, "save_draft_response",
                        {"tender_id": str(tid), "heading": "Copilot note", "body": content[:19000]}, channel="copilot_ui", allow_mutations=True), success="Saved to the bid workspace")

                async def send(text: str | None = None):
                    q = (text or box.value or "").strip()
                    if not q:
                        return
                    box.value = ""
                    status_line.text = "Thinking…"

                    async def on_step(step):
                        status_line.text = f"Running tool: {step['tool'].replace('_', ' ')}…"
                    try:
                        await copilot.ask(pc.ctx, state["cid"], q, on_step=on_step)
                    except AppError as exc:
                        ui.notify(exc.message, type="warning")
                    except Exception:
                        ui.notify("The copilot could not complete this request.", type="negative")
                    status_line.text = ""
                    render()

                render()
                with ui.row().classes("w-full no-wrap items-center"):
                    box = ui.input(placeholder="Ask e.g. “Can we qualify for this tender?”").props("outlined rounded").classes("col")
                    box.on("keydown.enter", lambda: send())
                    ui.button(icon="send", on_click=lambda: send()).props("round unelevated color=primary")

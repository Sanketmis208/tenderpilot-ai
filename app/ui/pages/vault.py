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
from app.ui.themes.theme import empty_state, page_header, panel

CATS = {c.value: c.value.replace("_", " ").capitalize() for c in DocumentCategory}
_CAT_HINTS = {"iso": "workspace_premium", "certif": "workspace_premium", "financ": "account_balance", "turnover": "account_balance", "balance": "account_balance",
              "gst": "receipt_long", "pan": "badge", "registr": "badge", "msme": "storefront", "udyam": "storefront", "complet": "task_alt", "work_order": "assignment",
              "experience": "task_alt", "catalog": "menu_book", "brochure": "menu_book", "tender": "gavel", "boq": "table_view", "power": "draw", "auth": "draw"}
CAT_ICON = {c: next((icon for k, icon in _CAT_HINTS.items() if k in c), "description") for c in CATS}


@ui.page("/vault")
def vault(request: Request):
    with shell(request, "/vault") as pc:
        if not pc:
            return
        with page_header("Document Vault", "Your qualification documents. Each file is versioned; assessments always reference the exact version they used."):
            pass
        flt = {"cat": None, "q": ""}

        @ui.refreshable
        def grid():
            with session_scope() as db:
                rows, total = documents.list_documents(db, pc.ctx, scope="company", category=flt["cat"], q=flt["q"] or None, limit=200)
                data = [(d.id, d.title, d.category, d.category_confidence, d.category_confirmed, v.version_no if v else None, v.processing_status if v else "—",
                         v.expires_on if v else None, v.needs_review if v else False, d.is_synthetic) for d, v in rows]
            if not data:
                with ui.card().classes("tp-card w-full").props("flat"):
                    empty_state("folder_open", "No documents yet", "Upload certificates, financial statements, completion certificates and catalogues.")
                return
            ui.label(f"{len(data)} document(s)").classes("tp-caption")
            with ui.element("div").classes("tp-grid-3 w-full"):
                for did, title, cat, conf, confirmed, vno, status, exp, review, synth in data:
                    with ui.card().classes("tp-card tp-panel cursor-pointer").props("flat").style("gap:10px").on("click", lambda did=did: ui.navigate.to(f"/vault/{did}")):
                        with ui.row().classes("w-full items-start justify-between no-wrap gap-3"):
                            with ui.row().classes("items-center no-wrap gap-3 col").style("min-width:0"):
                                ui.icon(CAT_ICON.get(cat, "description")).classes("tp-feature-icon tp-tone-primary").style("width:38px;height:38px;font-size:20px;flex:none")
                                with ui.column().classes("gap-0 col").style("min-width:0"):
                                    ui.label(title).classes("text-weight-medium ellipsis w-full").style("color:#101828")
                                    ui.label(CATS.get(cat, cat) + ("" if confirmed else f" · auto {conf:.0%}")).classes("tp-caption ellipsis w-full")
                            ui.badge(f"v{vno}", color="grey-7").props("outline").classes("tp-chip")
                        with ui.row().classes("gap-1"):
                            ui.badge(status.replace("_", " ").capitalize(), color="positive" if status == "ready" else "warning" if status in ("needs_review", "processing", "pending") else "negative").classes("tp-chip")
                            if review:
                                ui.badge("Confirm fields", color="info").classes("tp-chip")
                            if exp:
                                from datetime import date

                                ui.badge(("Expired " if exp < date.today() else "Valid to ") + exp.strftime("%d %b %Y"), color="negative" if exp < date.today() else "grey-7").props("outline").classes("tp-chip")
                            if synth:
                                ui.badge("Synthetic", color="orange").props("outline").classes("tp-chip")

        if pc.can(Perm.DOCUMENTS_UPLOAD):
            with panel("Upload documents", "Each file is classified automatically; uncertain fields are flagged for you to confirm.", icon="cloud_upload"):
                upload_box(pc, scope="company", on_done=grid.refresh)
        with ui.row().classes("w-full gap-2 items-center"):
            s = ui.input(placeholder="Search titles").props("outlined dense clearable").classes("col").style("min-width:220px")
            with s.add_slot("prepend"):
                ui.icon("search", size="18px").style("color:#98A2B3")
            s.on("update:model-value", lambda e: (flt.update(q=s.value or ""), grid.refresh()), throttle=0.4)
            ui.select({None: "All categories", **CATS}, value=None, on_change=lambda e: (flt.update(cat=e.value), grid.refresh())).props("outlined dense").style("width:260px")
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
            with page_header(title, CATS.get(cat, cat), back=("Document vault", "/vault")):
                    if cur:
                        ui.button("View", icon="visibility", on_click=lambda: ui.navigate.to(f"/viewer/{cur}")).props("outline color=primary")
                        download_button(pc, cur, props="outline color=primary")
                    if pc.can(Perm.DOCUMENTS_MANAGE):
                        ui.button("Delete", icon="delete_outline", on_click=lambda: confirm_delete()).props("outline color=negative")
            with ui.element("div").classes("tp-grid-2 w-full"):
                with panel("Classification", "Confirmed" if confirmed else f"Detected automatically ({conf:.0%} confidence)", icon="category"):
                    sel = ui.select(CATS, value=cat, label="Category").props("outlined dense").classes("w-full")
                    if pc.can(Perm.DOCUMENTS_UPLOAD):
                        ui.button("Confirm category", on_click=lambda: run(lambda db: documents.confirm_category(db, pc.ctx, did, sel.value), success="Category confirmed", refresh=body.refresh)).props("dense unelevated color=primary").classes("self-start")
                    ui.element("div").classes("tp-divider q-my-xs")
                    ui.label("Extracted fields").classes("tp-h2")
                    if not fields:
                        ui.label("No metadata fields extracted.").classes("tp-muted")
                    for fid, name, value, fconf, needs, confirmed_at in fields:
                        with ui.row().classes("w-full items-center justify-between no-wrap gap-2"):
                            ui.label(name.replace("_", " ").capitalize()).classes("tp-muted col")
                            inp = ui.input(value=value).props("dense outlined").style("width:240px")
                            if confirmed_at:
                                ui.icon("verified", color="positive").tooltip("Confirmed by a user")
                            elif pc.can(Perm.DOCUMENTS_UPLOAD):
                                ui.button(icon="check", on_click=lambda fid=fid, inp=inp: run(lambda db: documents.confirm_field(db, pc.ctx, fid, inp.value), success="Field confirmed", refresh=body.refresh)).props("dense flat color=positive").tooltip(f"Confirm ({fconf:.0%} extraction confidence)")
                with panel("Facts available to eligibility checks", "Document-stated values. Confirm them to raise confidence; reject incorrect extractions.", icon="fact_check"):
                    if not facts:
                        ui.label("No structured facts extracted from this document.").classes("tp-muted")
                    for kind, fid, desc, page, status, fconf in facts:
                        with ui.row().classes("w-full items-center justify-between no-wrap tp-row-line"):
                            with ui.column().classes("gap-1 col"):
                                ui.label(desc).classes("text-body2").style("color:#101828")
                                with ui.row().classes("items-center gap-2"):
                                    ui.badge(status.capitalize(), color="positive" if status == "confirmed" else "negative" if status == "rejected" else "grey-7").classes("tp-chip")
                                    ui.label(f"{kind} · p.{page} · {fconf:.0%} confidence").classes("tp-caption")
                            if pc.can(Perm.DOCUMENTS_UPLOAD) and status == "extracted":
                                ui.button(icon="check", on_click=lambda kind=kind, fid=fid: run(lambda db: _set_fact(db, pc, kind, fid, FactStatus.CONFIRMED), success="Confirmed", refresh=body.refresh)).props("dense flat color=positive")
                                ui.button(icon="close", on_click=lambda kind=kind, fid=fid: run(lambda db: _set_fact(db, pc, kind, fid, FactStatus.REJECTED), success="Rejected", refresh=body.refresh)).props("dense flat color=negative")
            with panel("Versions", f"{len(vers)} version(s) on file", icon="history"):
                for vid, vno, fname, status, at, sha, exp, iss, pages, method, notes, lang in vers:
                    with ui.row().classes("w-full items-center justify-between no-wrap tp-row-line gap-3"):
                        with ui.row().classes("items-center no-wrap gap-3 col").style("min-width:0"):
                            ui.badge(f"v{vno}", color="primary" if vid == cur else "grey-7").classes("tp-chip")
                            with ui.column().classes("gap-0 col").style("min-width:0"):
                                with ui.row().classes("items-center gap-2"):
                                    ui.label(fname).classes("text-weight-medium ellipsis").style("color:#101828")
                                    if vid == cur:
                                        ui.badge("Current", color="positive").classes("tp-chip")
                                ui.label(f"{at.astimezone(IST):%d %b %Y %H:%M} · {status} · {pages or '?'} p · {method or ''} · {lang or ''} · sha256 {sha[:10]}…").classes("tp-caption ellipsis")
                        with ui.row().classes("no-wrap"):
                            ui.button(icon="visibility", on_click=lambda vid=vid: ui.navigate.to(f"/viewer/{vid}")).props("flat dense")
                            download_button(pc, vid, label="")
                if pc.can(Perm.DOCUMENTS_UPLOAD):
                    ui.label("Upload a replacement (new version). Earlier assessments keep referencing the version they used.").classes("text-caption tp-muted q-mt-sm")
                    upload_box(pc, document_id=did, on_done=body.refresh, label="Upload new version")

        def confirm_delete():
            with ui.dialog() as dlg, ui.card().style("width:440px"):
                ui.icon("delete_outline").classes("tp-feature-icon tp-tone-rose")
                ui.label("Delete this document from the vault?").classes("tp-h1").style("font-size:1.2rem")
                ui.label("It will no longer be used as evidence. Historical assessments keep their snapshot.").classes("tp-muted")
                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button("Cancel", on_click=dlg.close).props("outline color=primary")
                    ui.button("Delete", on_click=lambda: (run(lambda db: documents.soft_delete(db, pc.ctx, did), success="Deleted"), dlg.close(), ui.navigate.to("/vault"))).props("unelevated color=negative")
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
        with page_header("Company Profile", "The facts TenderPilot uses for opportunity matching and eligibility checks."):
            pass
        with ui.element("div").classes("w-full").style("display:grid;grid-template-columns:repeat(auto-fit,minmax(min(380px,100%),1fr));gap:16px;align-items:start"):
            with panel("Profile", "Business details and registrations", icon="apartment"):
                f = {}
                with ui.element("div").classes("w-full").style("display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px"):
                    for key, label in [("legal_name", "Legal name"), ("industry", "Industry"), ("business_category", "Business category"), ("gstin", "GSTIN"), ("pan", "PAN"),
                                       ("cin", "CIN"), ("udyam_number", "Udyam"), ("msme_category", "MSME category"), ("state", "State")]:
                        f[key] = ui.input(label, value=vals.get(key)).props("outlined dense").classes("w-full")
                    f["year_established"] = ui.number("Year established", value=vals.get("year_established"), format="%d").props("outlined dense").classes("w-full")
                f["address"] = ui.input("Address", value=vals.get("address")).props("outlined dense").classes("w-full")
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
                with ui.row().classes("w-full items-center justify-between gap-2"):
                    ui.label("Identifiers are document-stated or user-entered and not verified with government registries.").classes("tp-caption col")
                    if pc.can(Perm.COMPANY_WRITE):
                        ui.button("Save profile", icon="save", on_click=save).props("unelevated color=primary")
            with ui.column().classes("w-full gap-4"):
                with panel("Certifications on file", f"{len(certs)} record(s)", icon="workspace_premium"):
                    from datetime import date

                    for t, n, vu, st in certs:
                        expired = bool(vu and vu < date.today())
                        with ui.row().classes("w-full items-center justify-between no-wrap tp-row-line gap-2"):
                            with ui.column().classes("gap-0 col"):
                                ui.label(f"{t} {n or ''}").classes("text-weight-medium").style("color:#101828")
                                ui.label(st).classes("tp-caption")
                            ui.badge(f"{'Expired' if expired else 'Valid to'} {vu or '?'}", color="negative" if expired else "positive").classes("tp-chip")
                    if not certs:
                        ui.label("None extracted yet.").classes("tp-muted")
                with panel("Turnover records", f"{len(fins)} financial year(s)", icon="account_balance"):
                    for fy, amt, aud, st in fins:
                        with ui.row().classes("w-full items-center justify-between no-wrap tp-row-line gap-2"):
                            with ui.row().classes("items-center gap-2"):
                                ui.label(f"FY {fy}").classes("text-weight-medium").style("color:#101828")
                                if aud:
                                    ui.badge("Audited", color="positive").props("outline").classes("tp-chip")
                                ui.label(st).classes("tp-caption")
                            ui.label(format_inr(amt)).classes("text-weight-medium tp-num").style("color:#101828")
                    if not fins:
                        ui.label("None extracted yet.").classes("tp-muted")
                with panel("Completed projects", f"{len(projects)} project(s)", icon="task_alt"):
                    for title, v, done in projects:
                        with ui.row().classes("w-full items-center justify-between no-wrap tp-row-line gap-3"):
                            with ui.column().classes("gap-0 col").style("min-width:0"):
                                ui.label(title[:110]).classes("text-weight-medium").style("color:#101828")
                                ui.label(f"Completed {done or 'date n/a'}").classes("tp-caption")
                            ui.label(format_inr(v) if v else "Value n/a").classes("text-weight-medium tp-num").style("color:#101828;white-space:nowrap")
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
        with ui.row().classes("w-full gap-4 no-wrap items-stretch").style("height: calc(100vh - 148px); min-height:520px"):
            with ui.card().classes("tp-card gt-sm").props("flat").style("width: 280px; padding:14px; gap:10px"):
                ui.label("Conversations").classes("tp-h2")
                tender_sel = ui.select({"": "No tender context", **tenders_opts}, value=tender_q or "", label="Tender context").props("outlined dense").classes("w-full")

                def new_conv():
                    from uuid import UUID

                    cid = run(lambda db: copilot.start_conversation(db, pc.ctx, UUID(tender_sel.value) if tender_sel.value else None).id)
                    if cid:
                        ui.navigate.to(f"/copilot?c={cid}")
                ui.button("New conversation", icon="add", on_click=new_conv).props("unelevated color=primary").classes("w-full")
                ui.label("Recent").classes("tp-nav-section").style("padding:8px 2px 0")
                with ui.scroll_area().classes("col"):
                    for cid, title, tid in convs:
                        with ui.element("a").props(f'href="/copilot?c={cid}"').classes("tp-nav-item" + (" active" if cid == state["cid"] else "")):
                            ui.icon("chat_bubble_outline", size="17px")
                            ui.label(title[:40]).classes("ellipsis")
                    if not convs:
                        ui.label("No conversations yet.").classes("tp-caption q-pa-sm")
            with ui.card().classes("tp-card col column no-wrap").props("flat").style("padding:0;gap:0;overflow:hidden"):
                if not state["cid"]:
                    with ui.column().classes("col w-full items-center justify-center"):
                        with ui.element("div").classes("tp-logo").style("width:56px;height:56px;border-radius:16px"):
                            ui.icon("auto_awesome", size="28px")
                        ui.label("Procurement Copilot").classes("tp-h1 q-mt-md")
                        ui.label("Ask about eligibility, evidence, deadlines or missing documents. Answers are grounded in your own records via "
                                 "TenderPilot tools, with sources. Pick a tender context and start a conversation.").classes("tp-subtitle text-center").style("max-width:520px")
                        ui.button("Start a conversation", icon="add", on_click=new_conv).props("unelevated color=primary").classes("q-mt-md")
                    return
                with ui.row().classes("w-full items-center no-wrap gap-3").style("padding:14px 20px;border-bottom:1px solid #EAECF0"):
                    with ui.element("div").classes("tp-logo"):
                        ui.icon("auto_awesome", size="18px")
                    with ui.column().classes("gap-0"):
                        ui.label(next((t for c, t, _ in convs if c == state["cid"]), "Conversation")[:80]).classes("tp-h2")
                        ui.label("Grounded in your records · cites sources · asks before changing anything").classes("tp-caption")
                msgs_area = ui.scroll_area().classes("col w-full").style("padding:8px 20px")
                stream_md = ui.markdown("").classes("text-body2 w-full q-px-lg")
                stream_md.set_visibility(False)
                status_line = ui.label("").classes("text-caption tp-muted q-px-lg")

                def render():
                    msgs_area.clear()
                    try:
                        with session_scope() as db:
                            msgs = [(m.id, m.role, m.content, m.citations, m.tool_calls, m.suggestions, m.mode) for m in copilot.messages(db, pc.ctx, state["cid"])]
                    except AppError as exc:
                        with msgs_area:
                            ui.label(exc.message).classes("text-negative")
                        return
                    last_q = ""
                    with msgs_area:
                        for mid, role, content, cites, calls, sugg, mode in msgs:
                            if role == "user":
                                last_q = content
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
                                            with ui.card().props("flat").style("padding:12px;background:#FFFAEB;border-color:#FEDF89;gap:6px"):
                                                ui.label(f"Proposed action: {c['tool'].replace('_', ' ')}").classes("text-weight-medium")
                                                ui.label("Requires your approval before anything is changed.").classes("text-caption tp-muted")
                                                ui.button("Approve & run", on_click=lambda mid=mid, i=i: run(lambda db: copilot.execute_pending_action(db, pc.ctx, mid, i), success="Action completed", refresh=render)).props("dense unelevated color=secondary")
                                        elif c.get("status") == "approved_executed":
                                            ui.label(f"✓ {c['tool'].replace('_', ' ')} approved and executed").classes("text-caption text-positive")
                                    with ui.row().classes("gap-1"):
                                        for s_ in sugg[:3]:
                                            ui.button(s_, on_click=lambda s_=s_: send(s_)).props("dense flat no-caps color=secondary").classes("text-caption")
                                        if pc.can(Perm.TASKS_WRITE) and mode != "error":
                                            ui.button("Save as note", icon="note_add", on_click=lambda content=content: save_to_workspace("note", content)).props("dense flat no-caps")
                                            ui.button("Create task", icon="add_task", on_click=lambda content=content, q=last_q: save_to_workspace("task", content, q)).props("dense flat no-caps")
                                        if pc.can(Perm.BIDS_WRITE) and mode != "error":
                                            ui.button("Draft response", icon="edit_note", on_click=lambda content=content, q=last_q: save_to_workspace("draft", content, q)).props("dense flat no-caps")
                    msgs_area.scroll_to(percent=1.0)

                def save_to_workspace(kind, content, question=""):
                    """Explicit user actions (the click is the approval): note, task or draft response from an answer."""
                    with session_scope() as db:
                        conv = copilot.get_conversation(db, pc.ctx, state["cid"])
                        tid = conv.tender_id
                    if not tid:
                        ui.notify("Attach a tender to this conversation to save into its bid workspace.", type="warning")
                        return
                    body = content.replace("_Rule-based answer — no language model is configured; results come directly from TenderPilot tools._", "").strip()[:19000]
                    if kind == "task":
                        name, args, msg = "create_remediation_task", {"tender_id": str(tid), "title": ("Follow up: " + (question or body))[:300], "description": body[:4000]}, "Task created"
                    else:
                        heading = "Copilot note" if kind == "note" else ("Draft response: " + (question or "copilot answer"))[:200]
                        name, args, msg = "save_draft_response", {"tender_id": str(tid), "heading": heading, "body": body}, "Saved to the bid workspace"
                    run(lambda db: call_tool(db, pc.ctx, name, args, channel="copilot_ui", allow_mutations=True), success=msg)

                async def send(text: str | None = None):
                    q = (text or box.value or "").strip()
                    if not q:
                        return
                    box.value = ""
                    status_line.text = "Thinking…"
                    streamed: list[str] = []

                    async def on_step(step):
                        if step.get("state") == "streaming":
                            streamed.append(step["text"])
                            stream_md.set_content("".join(streamed))
                            stream_md.set_visibility(True)
                            status_line.text = "Writing…"
                            return
                        streamed.clear()  # a tool call follows: discard any streamed preamble
                        stream_md.set_visibility(False)
                        status_line.text = f"Running tool: {step['tool'].replace('_', ' ')}…"
                    try:
                        await copilot.ask(pc.ctx, state["cid"], q, on_step=on_step)
                    except AppError as exc:
                        ui.notify(exc.message, type="warning")
                    except Exception:
                        ui.notify("The copilot could not complete this request.", type="negative")
                    status_line.text = ""
                    stream_md.set_visibility(False)
                    render()

                render()
                with ui.row().classes("w-full no-wrap items-center gap-2").style("padding:14px 20px;border-top:1px solid #EAECF0;background:#FCFCFD"):
                    box = ui.input(placeholder="Ask e.g. “Can we qualify for this tender?”").props("outlined").classes("col")
                    box.on("keydown.enter", lambda: send())
                    ui.button(icon="arrow_upward", on_click=lambda: send()).props("round unelevated color=primary").tooltip("Send")

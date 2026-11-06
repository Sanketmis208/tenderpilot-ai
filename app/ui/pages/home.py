"""Onboarding, dashboard, analytics, opportunities and global search."""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import quote

from fastapi import Request
from nicegui import ui
from sqlalchemy import or_, select

from app.auth.deps import ui_principal
from app.auth.permissions import Perm
from app.db.base import utcnow
from app.db.models import CompanyProfile, Document, Tender
from app.db.session import session_scope
from app.extraction.normalization.values import IST, format_inr_short
from app.services.analytics import service as analytics
from app.services.documents import service as documents
from app.services.organizations import org_service
from app.services.tenders import matching
from app.ui.components.common import upload_box
from app.ui.layouts.shell import require_ctx, run, shell
from app.ui.themes.theme import (
    BRAND,
    apply_theme,
    chart,
    empty_state,
    kpi,
    kpi_grid,
    page_header,
    panel,
    score_ring,
)

INDIAN_STATES = ["Andhra Pradesh", "Assam", "Bihar", "Chhattisgarh", "Delhi", "Goa", "Gujarat", "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka",
                 "Kerala", "Madhya Pradesh", "Maharashtra", "Odisha", "Punjab", "Rajasthan", "Tamil Nadu", "Telangana", "Uttar Pradesh", "Uttarakhand", "West Bengal"]


def _onboarding_bar() -> None:
    with ui.row().classes("w-full items-center justify-between no-wrap").style("padding:16px 28px;border-bottom:1px solid #EAECF0;background:#fff"):
        with ui.row().classes("items-center no-wrap gap-2"):
            with ui.element("div").classes("tp-logo"):
                ui.icon("explore", size="19px")
            ui.html('<span class="tp-wordmark">TenderPilot <span class="ai">AI</span></span>')
        ui.link("Sign out", "/auth/form/logout").classes("tp-back")


@ui.page("/onboarding")
def onboarding(request: Request):
    apply_theme()
    with session_scope() as db:
        p = ui_principal(request, db)
        if p is None:
            ui.navigate.to("/login")
            return
        has_org = p.ctx is not None
        uid = p.user.id
    _onboarding_bar()
    if not has_org:
        with ui.column().classes("w-full items-center").style("padding:56px 16px"):
            with ui.card().classes("tp-card tp-panel").props("flat").style("width:520px;max-width:100%;padding:32px"):
                ui.icon("domain_add").classes("tp-feature-icon tp-tone-primary")
                ui.label("Create your organization").classes("tp-h1")
                ui.label("Your shared workspace for tenders, qualification documents and bids.").classes("tp-muted q-mb-sm")
                name = ui.input("Company / organization name").props("outlined").classes("w-full")

                def create():
                    def do(db):
                        from app.core.config import get_settings
                        from app.core.security import hash_token
                        from app.db.models import User, UserSession

                        org = org_service.create_organization(db, db.get(User, uid), name.value or "")
                        db.flush()
                        sess = db.scalar(select(UserSession).where(UserSession.token_hash == hash_token(request.cookies.get(get_settings().session_cookie_name, ""))))
                        sess.active_organization_id = org.id
                    if run(do) is not None or True:
                        ui.navigate.to("/onboarding")

                ui.button("Continue", on_click=create).props("unelevated color=primary icon-right=arrow_forward").classes("w-full q-mt-sm")
        return
    pc = require_ctx(request)
    if pc is None:
        return
    with session_scope() as db:
        prof = db.scalar(select(CompanyProfile).where(CompanyProfile.organization_id == pc.ctx.organization_id))
        vals = {k: getattr(prof, k) for k in ("legal_name", "industry", "business_category", "gstin", "pan", "cin", "udyam_number", "msme_category",
                                              "address", "state", "operational_states", "year_established", "capabilities", "product_categories",
                                              "tender_interests", "typical_project_min", "typical_project_max")}
    with ui.column().classes("w-full items-center").style("padding:40px 16px 64px"):
        with ui.column().classes("gap-1 items-center text-center q-mb-md"):
            ui.label("Getting started").classes("tp-eyebrow")
            ui.label("Set up your company").classes("tp-h1").style("font-size:1.75rem")
            ui.label("Only what's needed to assess tenders. You can change everything later in Company Profile.").classes("tp-subtitle")
        with ui.card().classes("tp-card").props("flat").style("width: 860px; max-width: 100%; padding:8px 24px"):
            form: dict = {}
            with ui.stepper().props("vertical flat animated").classes("w-full") as stepper:
                with ui.step("Business details"):
                    with ui.grid(columns=2).classes("w-full gap-3"):
                        form["legal_name"] = ui.input("Legal business name", value=vals["legal_name"]).props("outlined")
                        form["industry"] = ui.input("Industry", value=vals["industry"]).props("outlined")
                        form["business_category"] = ui.select(["Manufacturer", "Trader / Dealer", "Contractor", "Service provider", "OEM"], label="Business category", value=vals["business_category"]).props("outlined")
                        form["year_established"] = ui.number("Year established", value=vals["year_established"], format="%d").props("outlined")
                        form["state"] = ui.select(INDIAN_STATES, label="Registered state", value=vals["state"]).props("outlined")
                        form["operational_states"] = ui.select(INDIAN_STATES, label="Operational states", multiple=True, value=vals["operational_states"] or []).props("outlined use-chips")
                    with ui.stepper_navigation():
                        ui.button("Next", on_click=stepper.next).props("unelevated color=primary")
                with ui.step("Registrations"):
                    ui.label("Identifiers are stored as you enter them or as stated in uploaded documents; they are not verified with government registries.").classes("text-caption tp-muted")
                    with ui.grid(columns=2).classes("w-full gap-3"):
                        form["gstin"] = ui.input("GSTIN", value=vals["gstin"]).props("outlined maxlength=15")
                        form["pan"] = ui.input("PAN", value=vals["pan"]).props("outlined maxlength=10")
                        form["cin"] = ui.input("CIN (if applicable)", value=vals["cin"]).props("outlined")
                        form["udyam_number"] = ui.input("Udyam registration (if MSME)", value=vals["udyam_number"]).props("outlined")
                        form["msme_category"] = ui.select(["micro", "small", "medium"], label="MSME category", value=vals["msme_category"], clearable=True).props("outlined")
                    with ui.stepper_navigation():
                        ui.button("Next", on_click=stepper.next).props("unelevated color=primary")
                        ui.button("Back", on_click=stepper.previous).props("flat")
                with ui.step("Capabilities & interests"):
                    form["capabilities"] = ui.input_chips("Capabilities", value=vals["capabilities"] or []).props("outlined").classes("w-full")
                    form["product_categories"] = ui.input_chips("Product / service categories", value=vals["product_categories"] or []).props("outlined").classes("w-full")
                    form["tender_interests"] = ui.input_chips("Tender interest keywords", value=vals["tender_interests"] or []).props("outlined").classes("w-full")
                    with ui.row().classes("w-full gap-3"):
                        form["typical_project_min"] = ui.number("Typical project size from (₹)", value=float(vals["typical_project_min"]) if vals["typical_project_min"] else None).props("outlined").classes("col")
                        form["typical_project_max"] = ui.number("to (₹)", value=float(vals["typical_project_max"]) if vals["typical_project_max"] else None).props("outlined").classes("col")
                    with ui.stepper_navigation():
                        def save_and_next():
                            if save(False):
                                stepper.next()
                        ui.button("Save & continue", on_click=save_and_next).props("unelevated color=primary")
                        ui.button("Back", on_click=stepper.previous).props("flat")
                with ui.step("Upload documents"):
                    ui.label("Upload registration, GST, MSME, ISO, financial statements, completion certificates and catalogues. "
                             "Each file is classified automatically; uncertain fields are flagged for you to confirm.").classes("tp-muted")
                    upload_box(pc, scope="company")
                    with ui.stepper_navigation():
                        ui.button("Finish", on_click=lambda: (save(True), ui.navigate.to("/dashboard"))).props("unelevated color=secondary")
                        ui.button("Back", on_click=stepper.previous).props("flat")

    def save(done: bool) -> bool:
        def do(db):
            pc.ctx.require(Perm.COMPANY_WRITE)
            prof = db.scalar(select(CompanyProfile).where(CompanyProfile.organization_id == pc.ctx.organization_id))
            for k, el in form.items():
                v = el.value
                if k in ("typical_project_min", "typical_project_max"):
                    v = Decimal(str(v)) if v not in (None, "") else None
                elif k == "year_established":
                    v = int(v) if v else None
                elif k in ("gstin", "pan", "cin", "udyam_number"):
                    v = (v or "").strip().upper() or None
                setattr(prof, k, v)
            prof.onboarding_step = 4 if not done else 5
            if done:
                prof.onboarding_completed_at = utcnow()
            return True
        return bool(run(do, success="Saved" if not done else None))


def _deadline_row(tid, title, dl) -> None:
    days = (dl - utcnow()).days
    local = dl.astimezone(IST)
    with ui.element("a").props(f'href="/tenders/{tid}"').classes("row items-center no-wrap gap-3 tp-row-line w-full").style("text-decoration:none"):
        with ui.column().classes("items-center gap-0 flex-none").style("width:46px;padding:4px 0;border-radius:8px;border:1px solid #EAECF0;background:#F9FAFB"):
            ui.label(f"{local:%b}").style("font-size:.65rem;font-weight:600;text-transform:uppercase;color:#D92D20;letter-spacing:.06em")
            ui.label(f"{local:%d}").style("font-size:1.05rem;font-weight:650;color:#101828;line-height:1.1")
        with ui.column().classes("gap-0 col").style("min-width:0"):
            ui.label(title).classes("text-weight-medium ellipsis").style("color:#101828")
            ui.label(f"Closes {local:%a, %d %b · %H:%M} IST").classes("tp-caption")
        ui.badge(f"{days}d left" if days >= 0 else "closed", color="negative" if days < 7 else "warning" if days < 14 else "grey-7").classes("tp-chip")


@ui.page("/dashboard")
def dashboard(request: Request):
    with shell(request, "/dashboard") as pc:
        if not pc:
            return
        with session_scope() as db:
            d = analytics.dashboard(db, pc.ctx)
            upcoming = [(t.id, t.title, t.submission_deadline) for t in d["upcoming"]]
            matches = [(m.score, t.id, t.title, m.reasons[:2]) for m, t in matching.list_matches(db, pc.ctx)[:5]]
        hour = utcnow().astimezone(IST).hour
        greeting = "Good morning" if hour < 12 else "Good afternoon" if hour < 17 else "Good evening"
        with page_header(f"{greeting}, {pc.user_name.split(' ')[0]}", f"Here's what's happening across {pc.org_name} today · {utcnow().astimezone(IST):%A, %d %B %Y}"):
            ui.button("Ask copilot", icon="auto_awesome", on_click=lambda: ui.navigate.to("/copilot")).props("outline color=primary")
            ui.button("Import tender", icon="add", on_click=lambda: ui.navigate.to("/tenders?new=1")).props("unelevated color=primary")
        k = d["kpis"]
        if not d["has_data"]:
            with panel():
                empty_state("rocket_launch", "No tenders yet", "Import your first tender document to see eligibility, evidence and deadlines here. "
                            "Numbers on this dashboard are always computed from your own records.", "Import a tender", lambda: ui.navigate.to("/tenders?new=1"))
            with ui.element("div").classes("tp-grid-3 w-full"):
                for icon, tone, title, body, href in [
                    ("folder_shared", "violet", "Upload qualifications", "Certificates, financials and completion certificates go in the vault once.", "/vault"),
                    ("apartment", "sky", "Complete your profile", "Capabilities and geography drive opportunity matching.", "/company"),
                    ("group_add", "green", "Invite your team", "Bid managers, reviewers and contributors work together.", "/team")]:
                    with ui.card().classes("tp-card tp-panel cursor-pointer").props("flat").on("click", lambda href=href: ui.navigate.to(href)):
                        ui.icon(icon).classes(f"tp-feature-icon tp-tone-{tone}").style("width:38px;height:38px;font-size:20px")
                        ui.label(title).classes("tp-h2")
                        ui.label(body).classes("tp-muted text-body2")
            return
        with kpi_grid():
            kpi("Tenders", str(k["tenders_total"]), "description", f"{k['relevant_opportunities']} relevant (≥60 match)", "/tenders", tone="primary")
            kpi("Active bid value", format_inr_short(Decimal(k["active_bid_value"])), "account_balance_wallet", f"{k['active_workspaces']} workspaces", "/bids", tone="violet")
            kpi("Upcoming deadlines", str(k["upcoming_deadlines"]), "event", "next 30 days", tone="amber")
            kpi("Awaiting review", str(k["awaiting_reviews"]), "rate_review", "assessments & bids", "/eligibility", tone="sky")
            kpi("Win rate", f"{k['win_rate']}%" if k["win_rate"] is not None else "—", "emoji_events", f"{k['won']} won · {k['lost']} lost", tone="green")
        with ui.element("div").classes("tp-grid-2 w-full"):
            with panel("Tender pipeline", "Tenders by lifecycle stage", icon="view_kanban"):
                order = ["discovered", "imported", "processing", "ready_for_review", "under_evaluation", "shortlisted", "bid_preparation", "awaiting_approval",
                         "ready_for_external_submission", "submitted_externally", "won", "lost"]
                stages = [s for s in order if d["pipeline"].get(s)]
                chart({"tooltip": {}, "grid": {"left": 8, "right": 24, "top": 8, "bottom": 8, "containLabel": True},
                       "xAxis": {"type": "value", "minInterval": 1}, "yAxis": {"type": "category", "data": [s.replace("_", " ").capitalize() for s in stages], "inverse": True},
                       "series": [{"type": "bar", "barMaxWidth": 22, "data": [d["pipeline"][s] for s in stages], "itemStyle": {"color": BRAND, "borderRadius": [0, 6, 6, 0]}}]}).classes("w-full").style("height:260px")
            with panel("Eligibility conditions", "Across latest assessments", icon="donut_large"):
                labels = {"verified_satisfied": ("Verified", "#12B76A"), "needs_human_review": ("Needs review", "#2E90FA"), "insufficient_evidence": ("Missing evidence", "#F79009"),
                          "conflicting_evidence": ("Conflicting", "#EF6820"), "verified_not_satisfied": ("Not satisfied", "#F04438"), "not_applicable": ("N/A", "#98A2B3")}
                data = [{"name": labels[k_][0], "value": v, "itemStyle": {"color": labels[k_][1]}} for k_, v in d["decisions"].items() if k_ in labels]
                if data:
                    total = sum(x["value"] for x in data)
                    chart({"tooltip": {"trigger": "item"}, "legend": {"orient": "vertical", "right": 0, "top": "middle"},
                           "title": {"text": str(total), "subtext": "conditions", "left": "29%", "top": "40%", "textAlign": "center",
                                     "textStyle": {"fontSize": 24, "fontWeight": 650, "color": "#101828"}, "subtextStyle": {"color": "#667085"}},
                           "series": [{"type": "pie", "center": ["30%", "50%"], "radius": ["58%", "80%"], "data": data, "label": {"show": False},
                                       "itemStyle": {"borderColor": "#fff", "borderWidth": 2}}]}).classes("w-full").style("height:260px")
                else:
                    empty_state("fact_check", "No assessments yet", "Run an eligibility assessment to see condition status.")
        with ui.element("div").classes("tp-grid-2 w-full"):
            with panel("Upcoming deadlines", "Next 30 days", icon="event", actions=lambda: ui.link("View all", "/tenders").classes("tp-link text-caption")):
                if not upcoming:
                    ui.label("No deadlines in the next 30 days.").classes("tp-muted")
                with ui.column().classes("w-full gap-0"):
                    for tid, title, dl in upcoming:
                        _deadline_row(tid, title, dl)
            with panel("Top opportunities", "Ranked by match relevance", icon="travel_explore", actions=lambda: ui.link("View all", "/opportunities").classes("tp-link text-caption")):
                if not matches:
                    ui.label("Open Opportunities to compute relevance scores.").classes("tp-muted")
                with ui.column().classes("w-full gap-0"):
                    for score, tid, title, reasons in matches:
                        with ui.element("a").props(f'href="/tenders/{tid}"').classes("row items-center no-wrap gap-3 tp-row-line w-full").style("text-decoration:none"):
                            score_ring(score)
                            with ui.column().classes("gap-0 col").style("min-width:0"):
                                ui.label(title).classes("text-weight-medium ellipsis").style("color:#101828")
                                ui.label(" · ".join(reasons)).classes("tp-caption ellipsis")
        if d["gap_categories"]:
            with panel("Eligibility gap categories", "Where your qualification evidence most often falls short", icon="report_problem"):
                chart({"tooltip": {}, "grid": {"left": 8, "right": 8, "top": 16, "bottom": 8, "containLabel": True},
                       "xAxis": {"type": "category", "data": [c.replace("_", " ").capitalize() for c in d["gap_categories"]]}, "yAxis": {"type": "value", "minInterval": 1},
                       "series": [{"type": "bar", "barMaxWidth": 40, "data": list(d["gap_categories"].values()), "itemStyle": {"color": "#F79009", "borderRadius": [6, 6, 0, 0]}}]}).classes("w-full").style("height:220px")


@ui.page("/analytics")
def analytics_page(request: Request):
    with shell(request, "/analytics") as pc:
        if not pc:
            return
        with session_scope() as db:
            d = analytics.dashboard(db, pc.ctx)
            costs = analytics.ai_cost_per_tender(db, pc.ctx)
        with page_header("Analytics", "All figures are computed from your organization's persisted records."):
            pass
        if not d["has_data"]:
            with panel():
                empty_state("insights", "Nothing to analyse yet", "Analytics appear once you import tenders and run assessments.")
            return
        k = d["kpis"]
        with kpi_grid():
            kpi("Submitted bids", str(k["submitted"]), "send", tone="primary")
            kpi("Won / lost", f"{k['won']} / {k['lost']}", "emoji_events", tone="green")
            kpi("Verified conditions", str(k["verified_conditions"]), "verified", tone="sky")
            kpi("Missing evidence items", str(k["missing_evidence"]), "report_problem", tone="amber")
            kpi("AI cost (30 days)", f"${k['ai_cost_usd_30d']:.2f}", "payments", f"{k['ai_tokens_30d']:,} tokens", tone="violet")
        with ui.element("div").classes("tp-grid-2 w-full"):
            with panel("Monthly activity", "Tenders imported vs. bids submitted", icon="bar_chart"):
                chart({"tooltip": {"trigger": "axis"}, "legend": {"bottom": 0}, "grid": {"left": 8, "right": 8, "top": 16, "bottom": 36, "containLabel": True},
                       "xAxis": {"type": "category", "data": [m["month"] for m in d["monthly"]]},
                       "yAxis": {"type": "value", "minInterval": 1}, "series": [
                           {"name": "Tenders imported", "type": "bar", "barMaxWidth": 18, "data": [m["tenders"] for m in d["monthly"]], "itemStyle": {"color": BRAND, "borderRadius": [4, 4, 0, 0]}},
                           {"name": "Submitted", "type": "bar", "barMaxWidth": 18, "data": [m["submitted"] for m in d["monthly"]], "itemStyle": {"color": "#A78BFA", "borderRadius": [4, 4, 0, 0]}}]}).classes("w-full").style("height:260px")
            with panel("Team workload", "Open tasks per assignee", icon="groups"):
                if d["workload"]:
                    chart({"tooltip": {}, "legend": {"bottom": 0}, "grid": {"left": 8, "right": 8, "top": 16, "bottom": 36, "containLabel": True},
                           "xAxis": {"type": "category", "data": [w["user"] for w in d["workload"]]}, "yAxis": {"type": "value", "minInterval": 1},
                           "series": [{"name": "Open", "type": "bar", "stack": "t", "barMaxWidth": 28, "data": [w["open"] - w["overdue"] for w in d["workload"]], "itemStyle": {"color": "#2E90FA"}},
                                      {"name": "Overdue", "type": "bar", "stack": "t", "barMaxWidth": 28, "data": [w["overdue"] for w in d["workload"]], "itemStyle": {"color": "#F04438", "borderRadius": [4, 4, 0, 0]}}]}).classes("w-full").style("height:260px")
                else:
                    empty_state("assignment_ind", "No assigned tasks yet", "Assign bid tasks to teammates to see workload here.")
        with panel("AI processing cost per tender", "Model usage recorded during extraction, assessment and drafting", icon="payments"):
            if costs:
                ui.table(columns=[{"name": "tender", "label": "Tender", "field": "tender", "align": "left"}, {"name": "cost", "label": "Cost (USD)", "field": "cost_usd", "align": "right"},
                                  {"name": "tokens", "label": "Tokens", "field": "tokens", "align": "right"}], rows=costs).props("flat").classes("w-full")
            else:
                ui.label("No model usage recorded — extraction ran with deterministic rules (no provider cost).").classes("tp-muted")


@ui.page("/opportunities")
def opportunities(request: Request):
    with shell(request, "/opportunities") as pc:
        if not pc:
            return

        @ui.refreshable
        def body():
            with session_scope() as db:
                rows = [(m.score, m.breakdown, m.reasons, m.user_action, t.id, t.title, t.reference_number, t.submission_deadline, t.estimated_value)
                        for m, t in matching.list_matches(db, pc.ctx)]
            if not rows:
                with panel():
                    empty_state("travel_explore", "No scored opportunities", "Import tenders (or a CSV feed), then recompute relevance.", "Recompute now",
                                lambda: run(lambda db: matching.recompute(db, pc.ctx), success="Relevance recomputed", refresh=body.refresh))
                return
            for score, br, reasons, action, tid, title, ref, dl, val in rows:
                with ui.card().classes("tp-card tp-panel w-full").props("flat"):
                    with ui.row().classes("w-full items-start no-wrap gap-4"):
                        score_ring(score)
                        with ui.column().classes("gap-1 col").style("min-width:0"):
                            with ui.row().classes("items-center gap-2"):
                                ui.link(title, f"/tenders/{tid}").classes("tp-link text-subtitle1").style("color:#101828")
                                if action == "shortlisted":
                                    ui.badge("Shortlisted", color="primary").classes("tp-chip")
                            with ui.row().classes("items-center gap-3 tp-caption"):
                                ui.label(ref or "No reference")
                                ui.label("·")
                                ui.label(format_inr_short(val) if val else "Value n/a")
                                if dl:
                                    ui.label("·")
                                    ui.label(f"Closes {dl.astimezone(IST):%d %b %Y} IST")
                            with ui.column().classes("gap-1 q-mt-xs"):
                                for r in reasons:
                                    with ui.row().classes("items-start no-wrap gap-2"):
                                        ui.icon("check", size="16px").style("color:#079455;margin-top:2px")
                                        ui.label(r).classes("text-body2")
                            with ui.row().classes("gap-1 q-mt-xs"):
                                for k_, v in br.items():
                                    ui.badge(f"{k_.replace('_', ' ')} {int(v * 100)}%", color="grey-7").props("outline").classes("tp-chip")
                        if pc.can(Perm.TENDERS_WRITE):
                            with ui.row().classes("gap-2 no-wrap"):
                                ui.button("Dismiss", on_click=lambda tid=tid: run(lambda db: matching.set_action(db, pc.ctx, tid, "dismissed"), success="Dismissed", refresh=body.refresh)).props("flat dense")
                                ui.button("Shortlist", icon="bookmark_add", on_click=lambda tid=tid: run(lambda db: matching.set_action(db, pc.ctx, tid, "shortlisted"), success="Shortlisted", refresh=body.refresh)).props("dense unelevated color=primary" if action != "shortlisted" else "dense outline color=primary disable")

        with page_header("Opportunities", "Relevance ranking from your capabilities, geography, experience and deadlines. Relevance is not verified eligibility."):
            ui.button("Recompute relevance", icon="refresh", on_click=lambda: run(lambda db: matching.recompute(db, pc.ctx), success="Relevance recomputed", refresh=body.refresh)).props("outline color=primary")
        body()


@ui.page("/search")
def search(request: Request):
    q = (request.query_params.get("q") or "").strip()
    with shell(request, "") as pc:
        if not pc:
            return
        with page_header(f"Results for “{q}”" if q else "Search", "Tender titles, document names and full-text + semantic passage search across your organization."):
            pass
        box = ui.input(placeholder="Search tenders, documents, passages…", value=q).props("outlined clearable autofocus").classes("w-full").style("max-width:720px")
        with box.add_slot("prepend"):
            ui.icon("search")
        box.on("keydown.enter", lambda: ui.navigate.to(f"/search?q={quote(box.value or '')}"))
        if not q:
            return
        with session_scope() as db:
            like = f"%{q[:100]}%"
            ts = db.scalars(select(Tender).where(Tender.organization_id == pc.ctx.organization_id, Tender.deleted_at.is_(None),
                                                 or_(Tender.title.ilike(like), Tender.reference_number.ilike(like), Tender.procuring_organization.ilike(like))).limit(20)).all()
            ds = db.scalars(select(Document).where(Document.organization_id == pc.ctx.organization_id, Document.deleted_at.is_(None), Document.title.ilike(like)).limit(20)).all()
            from app.ai.retrieval.search import hybrid_search

            hits = hybrid_search(db, pc.ctx, q, limit=8) if len(q) > 2 else []
            trows = [(t.id, t.title, t.reference_number) for t in ts]
            drows = [(d.id, d.title, d.category) for d in ds]
            hrows = [(h.version_id, h.page_no, h.document_title, h.text[:240]) for h in hits]
        with ui.element("div").classes("tp-grid-2 w-full"):
            with panel("Tenders", f"{len(trows)} match(es)", icon="description"):
                with ui.column().classes("w-full gap-0"):
                    for tid, title, ref in trows:
                        with ui.element("a").props(f'href="/tenders/{tid}"').classes("column tp-row-line w-full").style("text-decoration:none"):
                            ui.label(title).classes("text-weight-medium").style("color:#101828")
                            ui.label(ref or "No reference").classes("tp-caption")
                if not trows:
                    ui.label("No matching tenders.").classes("tp-muted")
            with panel("Documents", f"{len(drows)} match(es)", icon="folder_shared"):
                with ui.column().classes("w-full gap-0"):
                    for did, title, cat in drows:
                        with ui.element("a").props(f'href="/vault/{did}"').classes("column tp-row-line w-full").style("text-decoration:none"):
                            ui.label(title).classes("text-weight-medium").style("color:#101828")
                            ui.label(cat.replace("_", " ").capitalize()).classes("tp-caption")
                if not drows:
                    ui.label("No matching document titles.").classes("tp-muted")
        with panel("Passages", "Full-text + semantic search", icon="format_quote"):
            if not hrows:
                ui.label("No matching passages." if len(q) > 2 else "Type at least three characters to search passages.").classes("tp-muted")
            for vid, page, title, text in hrows:
                with ui.column().classes("w-full gap-1 q-mb-sm"):
                    with ui.row().classes("items-center gap-2"):
                        ui.icon("picture_as_pdf", size="16px").style("color:#D92D20")
                        ui.link(f"{title} — p.{page}", f"/viewer/{vid}?page={page}").classes("tp-link")
                    ui.label(text).classes("tp-quote w-full")


_ = documents  # used by upload_box via components

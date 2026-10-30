"""Onboarding, dashboard, analytics, opportunities and global search."""

from __future__ import annotations

from decimal import Decimal

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
from app.ui.themes.theme import NAVY, apply_theme, empty_state, kpi

INDIAN_STATES = ["Andhra Pradesh", "Assam", "Bihar", "Chhattisgarh", "Delhi", "Goa", "Gujarat", "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka",
                 "Kerala", "Madhya Pradesh", "Maharashtra", "Odisha", "Punjab", "Rajasthan", "Tamil Nadu", "Telangana", "Uttar Pradesh", "Uttarakhand", "West Bengal"]


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
    if not has_org:
        with ui.column().classes("w-full items-center q-pa-xl"):
            with ui.card().classes("tp-card q-pa-xl").style("width:520px"):
                ui.label("Create your organization").classes("tp-h1")
                ui.label("Your workspace for tenders, documents and bids.").classes("tp-muted q-mb-md")
                name = ui.input("Company / organization name").props("outlined").classes("w-full")

                def create():
                    def do(db):
                        from app.db.models import User, UserSession
                        from app.core.config import get_settings
                        from app.core.security import hash_token

                        org = org_service.create_organization(db, db.get(User, uid), name.value or "")
                        db.flush()
                        sess = db.scalar(select(UserSession).where(UserSession.token_hash == hash_token(request.cookies.get(get_settings().session_cookie_name, ""))))
                        sess.active_organization_id = org.id
                    if run(do) is not None or True:
                        ui.navigate.to("/onboarding")

                ui.button("Continue", on_click=create).props("unelevated color=primary").classes("w-full q-mt-md")
        return
    pc = require_ctx(request)
    if pc is None:
        return
    with session_scope() as db:
        prof = db.scalar(select(CompanyProfile).where(CompanyProfile.organization_id == pc.ctx.organization_id))
        vals = {k: getattr(prof, k) for k in ("legal_name", "industry", "business_category", "gstin", "pan", "cin", "udyam_number", "msme_category",
                                              "address", "state", "operational_states", "year_established", "capabilities", "product_categories",
                                              "tender_interests", "typical_project_min", "typical_project_max")}
    with ui.column().classes("w-full items-center q-pa-lg"):
        with ui.card().classes("tp-card q-pa-lg").style("width: 860px; max-width: 96vw"):
            ui.label("Set up your company").classes("tp-h1")
            ui.label("Only what's needed to assess tenders. You can change everything later in Company Profile.").classes("tp-muted")
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


@ui.page("/dashboard")
def dashboard(request: Request):
    with shell(request, "/dashboard") as pc:
        if not pc:
            return
        with session_scope() as db:
            d = analytics.dashboard(db, pc.ctx)
            upcoming = [(t.id, t.title, t.submission_deadline) for t in d["upcoming"]]
            matches = [(m.score, t.id, t.title, m.reasons[:2]) for m, t in matching.list_matches(db, pc.ctx)[:5]]
        with ui.row().classes("w-full items-center justify-between"):
            with ui.column().classes("gap-0"):
                ui.label(f"Good to see you, {pc.user_name.split(' ')[0]}").classes("tp-h1")
                ui.label(pc.org_name).classes("tp-muted")
            with ui.row():
                ui.button("Import tender", icon="add", on_click=lambda: ui.navigate.to("/tenders?new=1")).props("unelevated color=secondary")
        k = d["kpis"]
        if not d["has_data"]:
            with ui.card().classes("tp-card w-full"):
                empty_state("rocket_launch", "No tenders yet", "Import your first tender document to see eligibility, evidence and deadlines here. "
                            "Numbers on this dashboard are always computed from your own records.", "Import a tender", lambda: ui.navigate.to("/tenders?new=1"))
            return
        with ui.row().classes("w-full gap-4 no-wrap"):
            kpi("Tenders", str(k["tenders_total"]), "description", f"{k['relevant_opportunities']} relevant (≥60 match)", "/tenders")
            kpi("Active bid value", format_inr_short(Decimal(k["active_bid_value"])), "account_balance_wallet", f"{k['active_workspaces']} workspaces", "/bids")
            kpi("Upcoming deadlines", str(k["upcoming_deadlines"]), "event", "next 30 days")
            kpi("Awaiting review", str(k["awaiting_reviews"]), "rate_review", "assessments & bids", "/eligibility")
            kpi("Win rate", f"{k['win_rate']}%" if k["win_rate"] is not None else "—", "emoji_events", f"{k['won']} won · {k['lost']} lost")
        with ui.row().classes("w-full gap-4"):
            with ui.card().classes("tp-card col q-pa-md"):
                ui.label("Tender pipeline").classes("tp-h2")
                order = ["discovered", "imported", "processing", "ready_for_review", "under_evaluation", "shortlisted", "bid_preparation", "awaiting_approval",
                         "ready_for_external_submission", "submitted_externally", "won", "lost"]
                stages = [s for s in order if d["pipeline"].get(s)]
                ui.echart({"tooltip": {}, "grid": {"left": 150, "right": 20, "top": 10, "bottom": 20},
                           "xAxis": {"type": "value", "minInterval": 1}, "yAxis": {"type": "category", "data": [s.replace("_", " ") for s in stages], "inverse": True},
                           "series": [{"type": "bar", "data": [d["pipeline"][s] for s in stages], "itemStyle": {"color": NAVY, "borderRadius": 4}}]}).style("height:260px")
            with ui.card().classes("tp-card col q-pa-md"):
                ui.label("Eligibility conditions (latest assessments)").classes("tp-h2")
                labels = {"verified_satisfied": ("Verified", "#1E8E5A"), "needs_human_review": ("Needs review", "#2E86C1"), "insufficient_evidence": ("Missing evidence", "#D4A017"),
                          "conflicting_evidence": ("Conflicting", "#E67E22"), "verified_not_satisfied": ("Not satisfied", "#C0392B"), "not_applicable": ("N/A", "#8395A7")}
                data = [{"name": labels[k_][0], "value": v, "itemStyle": {"color": labels[k_][1]}} for k_, v in d["decisions"].items() if k_ in labels]
                if data:
                    ui.echart({"tooltip": {"trigger": "item"}, "legend": {"bottom": 0}, "series": [{"type": "pie", "radius": ["45%", "70%"], "data": data, "label": {"show": False}}]}).style("height:260px")
                else:
                    ui.label("Run an eligibility assessment to see condition status.").classes("tp-muted")
        with ui.row().classes("w-full gap-4"):
            with ui.card().classes("tp-card col q-pa-md"):
                ui.label("Upcoming deadlines").classes("tp-h2")
                if not upcoming:
                    ui.label("No deadlines in the next 30 days.").classes("tp-muted")
                for tid, title, dl in upcoming:
                    days = (dl - utcnow()).days
                    with ui.row().classes("w-full items-center justify-between q-py-xs no-wrap"):
                        ui.link(title[:70], f"/tenders/{tid}").classes("tp-link")
                        ui.badge(f"{dl.astimezone(IST):%d %b %H:%M} IST · {days}d", color="negative" if days < 7 else "grey-7").props("outline")
            with ui.card().classes("tp-card col q-pa-md"):
                ui.label("Top opportunities").classes("tp-h2")
                if not matches:
                    ui.label("Open Opportunities to compute relevance scores.").classes("tp-muted")
                for score, tid, title, reasons in matches:
                    with ui.column().classes("w-full q-py-xs gap-0"):
                        with ui.row().classes("w-full justify-between no-wrap"):
                            ui.link(title[:70], f"/tenders/{tid}").classes("tp-link")
                            ui.label(f"{score:.0f}/100").classes("text-weight-bold")
                        ui.label(" · ".join(reasons)).classes("text-caption tp-muted")
        if d["gap_categories"]:
            with ui.card().classes("tp-card w-full q-pa-md"):
                ui.label("Eligibility gap categories").classes("tp-h2")
                ui.echart({"tooltip": {}, "xAxis": {"type": "category", "data": list(d["gap_categories"])}, "yAxis": {"type": "value", "minInterval": 1},
                           "series": [{"type": "bar", "data": list(d["gap_categories"].values()), "itemStyle": {"color": "#D4A017", "borderRadius": 4}}]}).style("height:220px")


@ui.page("/analytics")
def analytics_page(request: Request):
    with shell(request, "/analytics") as pc:
        if not pc:
            return
        with session_scope() as db:
            d = analytics.dashboard(db, pc.ctx)
            costs = analytics.ai_cost_per_tender(db, pc.ctx)
        ui.label("Analytics").classes("tp-h1")
        ui.label("All figures are computed from your organization's persisted records.").classes("tp-muted")
        if not d["has_data"]:
            with ui.card().classes("tp-card w-full"):
                empty_state("insights", "Nothing to analyse yet", "Analytics appear once you import tenders and run assessments.")
            return
        k = d["kpis"]
        with ui.row().classes("w-full gap-4 no-wrap"):
            kpi("Submitted bids", str(k["submitted"]), "send")
            kpi("Won / lost", f"{k['won']} / {k['lost']}", "emoji_events")
            kpi("Verified conditions", str(k["verified_conditions"]), "verified")
            kpi("Missing evidence items", str(k["missing_evidence"]), "report_problem")
            kpi("AI cost (30 days)", f"${k['ai_cost_usd_30d']:.2f}", "payments", f"{k['ai_tokens_30d']:,} tokens")
        with ui.row().classes("w-full gap-4"):
            with ui.card().classes("tp-card col q-pa-md"):
                ui.label("Monthly activity").classes("tp-h2")
                ui.echart({"tooltip": {"trigger": "axis"}, "legend": {"bottom": 0}, "xAxis": {"type": "category", "data": [m["month"] for m in d["monthly"]]},
                           "yAxis": {"type": "value", "minInterval": 1}, "series": [
                               {"name": "Tenders imported", "type": "bar", "data": [m["tenders"] for m in d["monthly"]], "itemStyle": {"color": NAVY}},
                               {"name": "Submitted", "type": "bar", "data": [m["submitted"] for m in d["monthly"]], "itemStyle": {"color": "#0F9D8F"}}]}).style("height:260px")
            with ui.card().classes("tp-card col q-pa-md"):
                ui.label("Team workload (open tasks)").classes("tp-h2")
                if d["workload"]:
                    ui.echart({"tooltip": {}, "legend": {"bottom": 0}, "xAxis": {"type": "category", "data": [w["user"] for w in d["workload"]]}, "yAxis": {"type": "value", "minInterval": 1},
                               "series": [{"name": "Open", "type": "bar", "stack": "t", "data": [w["open"] - w["overdue"] for w in d["workload"]], "itemStyle": {"color": "#2E86C1"}},
                                          {"name": "Overdue", "type": "bar", "stack": "t", "data": [w["overdue"] for w in d["workload"]], "itemStyle": {"color": "#C0392B"}}]}).style("height:260px")
                else:
                    ui.label("No assigned tasks yet.").classes("tp-muted")
        with ui.card().classes("tp-card w-full q-pa-md"):
            ui.label("AI processing cost per tender").classes("tp-h2")
            if costs:
                ui.table(columns=[{"name": "tender", "label": "Tender", "field": "tender", "align": "left"}, {"name": "cost", "label": "Cost (USD)", "field": "cost_usd"},
                                  {"name": "tokens", "label": "Tokens", "field": "tokens"}], rows=costs).props("flat dense").classes("w-full")
            else:
                ui.label("No model usage recorded — extraction ran with deterministic rules (no provider cost).").classes("tp-muted")


@ui.page("/opportunities")
def opportunities(request: Request):
    with shell(request, "/opportunities") as pc:
        if not pc:
            return
        ui.label("Opportunities").classes("tp-h1")
        ui.label("Relevance ranking from your capabilities, geography, experience and deadlines. Relevance is not verified eligibility.").classes("tp-muted")

        @ui.refreshable
        def body():
            with session_scope() as db:
                rows = [(m.score, m.breakdown, m.reasons, m.user_action, t.id, t.title, t.reference_number, t.submission_deadline, t.estimated_value)
                        for m, t in matching.list_matches(db, pc.ctx)]
            if not rows:
                empty_state("travel_explore", "No scored opportunities", "Import tenders (or a CSV feed), then recompute relevance.", "Recompute now",
                            lambda: run(lambda db: matching.recompute(db, pc.ctx), success="Relevance recomputed", refresh=body.refresh))
                return
            for score, br, reasons, action, tid, title, ref, dl, val in rows:
                with ui.card().classes("tp-card w-full q-pa-md"):
                    with ui.row().classes("w-full items-start justify-between no-wrap"):
                        with ui.column().classes("gap-1 col"):
                            ui.link(title, f"/tenders/{tid}").classes("tp-link text-subtitle1")
                            ui.label(f"{ref or '—'} · {format_inr_short(val) if val else 'value n/a'} · closes {dl.astimezone(IST):%d %b %Y} IST" if dl else f"{ref or '—'}").classes("text-caption tp-muted")
                            for r in reasons:
                                ui.label(f"• {r}").classes("text-body2")
                        with ui.column().classes("items-end gap-1"):
                            ui.label(f"{score:.0f}").classes("text-h4 text-weight-bold").style(f"color:{NAVY}")
                            ui.label("match relevance").classes("text-caption tp-muted")
                            with ui.row().classes("gap-1"):
                                if pc.can(Perm.TENDERS_WRITE):
                                    ui.button("Shortlist", on_click=lambda tid=tid: run(lambda db: matching.set_action(db, pc.ctx, tid, "shortlisted"), success="Shortlisted", refresh=body.refresh)).props("dense unelevated color=secondary" if action != "shortlisted" else "dense flat disable")
                                    ui.button("Dismiss", on_click=lambda tid=tid: run(lambda db: matching.set_action(db, pc.ctx, tid, "dismissed"), success="Dismissed", refresh=body.refresh)).props("dense flat")
                    with ui.row().classes("gap-2 q-mt-sm"):
                        for k_, v in br.items():
                            ui.badge(f"{k_} {int(v * 100)}%", color="grey-7").props("outline")

        ui.button("Recompute relevance", icon="refresh", on_click=lambda: run(lambda db: matching.recompute(db, pc.ctx), success="Relevance recomputed", refresh=body.refresh)).props("outline color=primary")
        body()


@ui.page("/search")
def search(request: Request):
    q = (request.query_params.get("q") or "").strip()
    with shell(request, "") as pc:
        if not pc:
            return
        ui.label(f"Search results for “{q}”").classes("tp-h1")
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
        with ui.card().classes("tp-card w-full q-pa-md"):
            ui.label("Tenders").classes("tp-h2")
            for tid, title, ref in trows:
                ui.link(f"{title} ({ref or '—'})", f"/tenders/{tid}").classes("tp-link")
            if not trows:
                ui.label("No matching tenders.").classes("tp-muted")
        with ui.card().classes("tp-card w-full q-pa-md"):
            ui.label("Documents").classes("tp-h2")
            for did, title, cat in drows:
                ui.link(f"{title} · {cat.replace('_', ' ')}", f"/vault/{did}").classes("tp-link")
            if not drows:
                ui.label("No matching document titles.").classes("tp-muted")
        with ui.card().classes("tp-card w-full q-pa-md"):
            ui.label("Passages (full-text + semantic search)").classes("tp-h2")
            for vid, page, title, text in hrows:
                ui.link(f"{title} — p.{page}", f"/viewer/{vid}?page={page}").classes("tp-link")
                ui.label(text).classes("tp-quote q-mb-sm")


_ = documents  # used by upload_box via components

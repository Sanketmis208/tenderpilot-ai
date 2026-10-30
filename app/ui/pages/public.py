"""Landing page and authentication flows."""

from __future__ import annotations

import html

from fastapi import Request
from nicegui import ui

from app.core.errors import AppError
from app.db.session import session_scope
from app.services.billing.entitlements import PLAN_DEFINITIONS
from app.services.organizations import auth_service, org_service
from app.ui.themes.theme import NAVY, TEAL, apply_theme

FEATURES = [
    ("hub", "Requirements-to-Evidence graph", "Every tender condition is linked to the exact page and passage in your own documents — or flagged as missing."),
    ("rule", "Deterministic eligibility rules", "Turnover, validity dates and thresholds are checked with exact arithmetic, never guessed by a language model."),
    ("description", "Tender document intelligence", "PDF, DOCX, XLSX and ZIP tenders become structured requirements with source citations and confidence."),
    ("workspaces", "Collaborative bid rooms", "Tasks, reviews and approvals with version history and a full audit trail."),
    ("article", "Draft generation", "Technical responses, compliance matrices and checklists built only from verified company records."),
    ("auto_awesome", "Procurement copilot", "Ask questions about any tender and get answers grounded in your evidence, with sources."),
]
STEPS = [("upload", "Upload qualifications once", "Certificates, financials, completion certificates and catalogues."),
         ("cloud_upload", "Import a tender", "Upload the NIT/RFP, annexures, BOQ and corrigenda you downloaded."),
         ("fact_check", "Review the assessment", "See what is supported, missing, expired or conflicting — with evidence."),
         ("inventory_2", "Prepare the bid", "Assign remediation tasks, approve drafts, export a reviewable package.")]
FAQ = [
    ("Does TenderPilot submit bids on government portals?", "No. TenderPilot prepares an auditable package for you to submit through the official portal. It does not log in to portals, bypass CAPTCHAs or apply digital signatures."),
    ("Is my eligibility verified with government registries?", "No. TenderPilot checks your uploaded documents. Certificates and registrations are shown as document-stated; external authenticity verification is a separate step."),
    ("How do you avoid AI hallucinations?", "Eligibility decisions come from a deterministic rule engine over extracted facts with citations. AI is used for extraction and drafting assistance, and unsupported findings are never marked verified."),
    ("Is our data isolated from other companies?", "Yes. Every record, file, search index and AI tool call is scoped to your organization and enforced server-side."),
]


def _nav_bar() -> None:
    with ui.row().classes("w-full items-center justify-between q-px-xl q-py-md").style("max-width:1200px;margin:0 auto"):
        with ui.row().classes("items-center gap-2"):
            ui.icon("explore", size="28px").style(f"color:{TEAL}")
            ui.label("TenderPilot AI").classes("text-h6 text-weight-bold text-white")
        with ui.row().classes("items-center gap-2"):
            ui.link("Features", "#features").classes("text-white").style("text-decoration:none")
            ui.link("Pricing", "#pricing").classes("text-white q-ml-md").style("text-decoration:none")
            ui.button("Sign in", on_click=lambda: ui.navigate.to("/login")).props("flat color=white").classes("q-ml-md")
            ui.button("Start free trial", on_click=lambda: ui.navigate.to("/register")).props("unelevated color=secondary")


@ui.page("/")
def landing():
    apply_theme()
    with ui.column().classes("w-full tp-landing-hero q-pb-xl"):
        _nav_bar()
        with ui.row().classes("w-full q-px-xl q-pt-xl items-center no-wrap gap-8").style("max-width:1200px;margin:0 auto"):
            with ui.column().classes("col gap-4"):
                ui.label("Tender intelligence for Indian suppliers & contractors").classes("text-overline").style(f"color:{TEAL}")
                ui.label("Discover Smarter. Qualify Faster. Bid with Confidence.").classes("text-h3 text-weight-bold").style("line-height:1.15")
                ui.label("Upload your qualifications once. TenderPilot reads every tender, checks each eligibility condition against your own "
                         "documents with page-level evidence, and prepares a reviewable bid package.").classes("text-subtitle1").style("color:#C9D3E3;max-width:560px")
                with ui.row().classes("gap-3 q-mt-md"):
                    ui.button("Start free trial", on_click=lambda: ui.navigate.to("/register")).props("unelevated size=lg color=secondary")
                    ui.button("Sign in", on_click=lambda: ui.navigate.to("/login")).props("outline size=lg color=white")
            with ui.card().classes("col tp-card q-pa-md gt-sm").style("max-width:520px;color:#1B2333"):
                ui.label("Eligibility preview (illustrative)").classes("text-caption tp-muted")
                for code, title, label, color in [("R-001", "Avg. turnover ≥ ₹5 Cr (3 FYs)", "Conflicting evidence", "orange"),
                                                  ("R-002", "3 years experience", "Verified satisfied", "positive"),
                                                  ("R-004", "Valid ISO 9001", "Not satisfied — expired", "negative"),
                                                  ("R-008", "EMD exemption for MSEs", "Needs review", "info")]:
                    with ui.row().classes("w-full items-center justify-between q-py-xs no-wrap"):
                        ui.label(f"{code}  {title}").classes("text-body2")
                        ui.badge(label, color=color).props("rounded")
                ui.label("Each finding links to the exact page of your document.").classes("text-caption tp-muted q-mt-sm")
    with ui.column().classes("w-full q-px-xl q-py-xl items-center").style("max-width:1200px;margin:0 auto") as feat:
        feat.props('id="features"')
        ui.label("Built for the way tenders actually work").classes("text-h4 text-weight-bold").style(f"color:{NAVY}")
        with ui.grid(columns=3).classes("w-full gap-4 q-mt-lg"):
            for icon, title, body in FEATURES:
                with ui.card().classes("tp-card q-pa-lg"):
                    ui.icon(icon, size="30px").classes("text-secondary")
                    ui.label(title).classes("tp-h2 q-mt-sm")
                    ui.label(body).classes("tp-muted")
        ui.label("How it works").classes("text-h5 text-weight-bold q-mt-xl").style(f"color:{NAVY}")
        with ui.grid(columns=4).classes("w-full gap-4 q-mt-md"):
            for i, (icon, title, body) in enumerate(STEPS, 1):
                with ui.column().classes("q-pa-md"):
                    ui.label(f"{i:02d}").classes("text-h5 text-weight-bold text-secondary")
                    ui.label(title).classes("tp-h2")
                    ui.label(body).classes("tp-muted")
    with ui.column().classes("w-full q-py-xl items-center").style("background:#fff"):
        with ui.column().classes("q-px-xl items-center w-full").style("max-width:1200px") as pr:
            pr.props('id="pricing"')
            ui.label("Pricing").classes("text-h4 text-weight-bold").style(f"color:{NAVY}")
            ui.label("14-day trial on Professional. Prices exclude GST.").classes("tp-muted")
            with ui.row().classes("w-full gap-4 q-mt-lg justify-center"):
                for p in PLAN_DEFINITIONS:
                    with ui.card().classes("tp-card q-pa-lg col").style("max-width:340px"):
                        ui.label(p["name"]).classes("tp-h2")
                        ui.label(f"₹{p['price_inr_monthly']:,.0f}/month" if p["price_inr_monthly"] else "Custom").classes("text-h5 text-weight-bold")
                        lim = p["limits"]
                        for line in (f"{lim['members']} team members", f"{lim['tenders_analysed_per_month']} tender analyses / month",
                                     f"{lim['documents_processed_per_month']} documents / month", f"{lim['storage_bytes'] // 1024**3} GB storage"):
                            ui.label(f"✓ {line}").classes("text-body2")
                        ui.button("Contact sales" if not p["price_inr_monthly"] else "Start trial", on_click=lambda: ui.navigate.to("/register")).props("outline color=primary").classes("q-mt-md")
    with ui.column().classes("w-full q-px-xl q-py-xl").style("max-width:900px;margin:0 auto"):
        ui.label("Frequently asked questions").classes("text-h5 text-weight-bold").style(f"color:{NAVY}")
        for q, a in FAQ:
            with ui.expansion(q).classes("tp-card q-my-sm w-full"):
                ui.label(a).classes("tp-muted q-pa-sm")
    with ui.row().classes("w-full justify-center q-pa-lg").style(f"background:{NAVY};color:#8EA0BC"):
        ui.label("© TenderPilot AI · Not affiliated with any government procurement portal · Not legal advice")


def _auth_card(title: str, subtitle: str):
    apply_theme()
    ui.query("body").style(f"background: linear-gradient(160deg, {NAVY} 0%, #0B1528 100%)")
    with ui.column().classes("w-full items-center justify-center").style("min-height: 100vh"):
        with ui.row().classes("items-center gap-2 q-mb-md"):
            ui.icon("explore", size="30px").style(f"color:{TEAL}")
            ui.label("TenderPilot AI").classes("text-h6 text-white text-weight-bold")
        card = ui.card().classes("tp-card q-pa-xl").style("width: 420px; max-width: 94vw")
        with card:
            ui.label(title).classes("tp-h1")
            ui.label(subtitle).classes("tp-muted q-mb-md")
        return card


def _error_banner(request: Request) -> None:
    err = request.query_params.get("error")
    if err:
        ui.label("Request blocked: please submit the form from this site." if err == "origin" else err).classes("text-negative text-body2 q-mb-sm")


@ui.page("/login")
def login(request: Request):
    with _auth_card("Sign in", "Welcome back to TenderPilot AI."):
        _error_banner(request)
        with ui.element("form").props('method="post" action="/auth/form/login"').classes("w-full column gap-3"):
            ui.input("Email").props('name=email type=email outlined autocomplete=email autofocus').classes("w-full")
            ui.input("Password", password=True, password_toggle_button=True).props('name=password outlined autocomplete=current-password').classes("w-full")
            ui.html(f'<input type="hidden" name="next" value="{html.escape(request.query_params.get("next", "/dashboard"), quote=True)}">', sanitize=False)
            ui.button("Sign in").props("type=submit unelevated color=primary size=md").classes("w-full")
        with ui.row().classes("w-full justify-between q-mt-md"):
            ui.link("Forgot password?", "/forgot-password").classes("tp-link")
            ui.link("Create an account", "/register").classes("tp-link")


@ui.page("/register")
def register(request: Request):
    invite = request.query_params.get("invite")
    org_name = None
    if invite:
        with session_scope() as db:
            try:
                inv = org_service.get_invitation(db, invite)
                from app.db.models import Organization

                org_name = db.get(Organization, inv.organization_id).name
            except AppError:
                invite = None
    with _auth_card("Create your account", f"Join {org_name} on TenderPilot AI." if org_name else "Start your 14-day Professional trial."):
        _error_banner(request)
        with ui.element("form").props('method="post" action="/auth/form/register"').classes("w-full column gap-3"):
            ui.input("Full name").props("name=full_name outlined autocomplete=name").classes("w-full")
            ui.input("Work email").props("name=email type=email outlined autocomplete=email").classes("w-full")
            ui.input("Password", password=True, password_toggle_button=True).props("name=password outlined autocomplete=new-password hint='At least 10 characters with upper, lower case and a digit'").classes("w-full")
            if invite:
                ui.html(f'<input type="hidden" name="invite" value="{html.escape(invite, quote=True)}"><input type="hidden" name="organization_name" value="">', sanitize=False)
            else:
                ui.input("Company name").props("name=organization_name outlined").classes("w-full")
            ui.button("Create account").props("type=submit unelevated color=primary").classes("w-full")
        ui.link("Already have an account? Sign in", "/login").classes("tp-link q-mt-md")


@ui.page("/forgot-password")
def forgot():
    with _auth_card("Reset your password", "We'll email you a link if an account exists."):
        email = ui.input("Email").props("outlined type=email").classes("w-full")

        def submit():
            with session_scope() as db:
                auth_service.request_password_reset(db, email.value or "")
            ui.notify("If an account exists for that email, a reset link has been sent.", type="info")

        ui.button("Send reset link", on_click=submit).props("unelevated color=primary").classes("w-full q-mt-md")
        ui.link("Back to sign in", "/login").classes("tp-link q-mt-md")


@ui.page("/reset-password")
def reset(request: Request):
    token = request.query_params.get("token", "")
    with _auth_card("Choose a new password", "All existing sessions will be signed out."):
        pw = ui.input("New password", password=True, password_toggle_button=True).props("outlined").classes("w-full")

        def submit():
            try:
                with session_scope() as db:
                    auth_service.reset_password(db, token, pw.value or "")
                ui.notify("Password updated. Please sign in.", type="positive")
                ui.navigate.to("/login")
            except AppError as exc:
                ui.notify(exc.message, type="negative")

        ui.button("Update password", on_click=submit).props("unelevated color=primary").classes("w-full q-mt-md")


@ui.page("/verify-email")
def verify(request: Request):
    with _auth_card("Email verification", ""):
        try:
            with session_scope() as db:
                auth_service.verify_email(db, request.query_params.get("token", ""))
            ui.label("Your email address is verified.").classes("text-positive")
        except AppError as exc:
            ui.label(exc.message).classes("text-negative")
        ui.link("Continue", "/dashboard").classes("tp-link q-mt-md")


@ui.page("/invite")
def invite_page(request: Request):
    token = request.query_params.get("token", "")
    with _auth_card("Team invitation", ""):
        try:
            with session_scope() as db:
                inv = org_service.get_invitation(db, token)
                from app.auth.deps import ui_principal
                from app.db.models import Organization

                org = db.get(Organization, inv.organization_id)
                p = ui_principal(request, db)
                ui.label(f"You've been invited to join {org.name} as {inv.role.replace('_', ' ')}.")
                if p:
                    def accept():
                        try:
                            with session_scope() as db2:
                                from sqlalchemy import select

                                from app.core.security import hash_token
                                from app.db.models import User, UserSession

                                m = org_service.accept_invitation(db2, token, db2.get(User, p.user.id))
                                from app.core.config import get_settings

                                sess = db2.scalar(select(UserSession).where(UserSession.token_hash == hash_token(request.cookies.get(get_settings().session_cookie_name, ""))))
                                if sess:
                                    sess.active_organization_id = m.organization_id
                            ui.navigate.to("/dashboard")
                        except AppError as exc:
                            ui.notify(exc.message, type="negative")

                    ui.button("Accept invitation", on_click=accept).props("unelevated color=primary").classes("w-full q-mt-md")
                else:
                    ui.button("Create account to join", on_click=lambda: ui.navigate.to(f"/register?invite={token}")).props("unelevated color=primary").classes("w-full q-mt-md")
                    ui.link("I already have an account", f"/login?next=/invite?token={token}").classes("tp-link q-mt-sm")
        except AppError as exc:
            ui.label(exc.message).classes("text-negative")

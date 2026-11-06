"""Landing page and authentication flows."""

from __future__ import annotations

import html

from fastapi import Request
from nicegui import ui

from app.core.errors import AppError
from app.db.session import session_scope
from app.services.billing.entitlements import PLAN_DEFINITIONS
from app.services.organizations import auth_service, org_service
from app.ui.themes.theme import apply_theme, score_ring

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


def _logo(dark: bool = False) -> None:
    with ui.element("a").props('href="/"').classes("row items-center no-wrap gap-2").style("text-decoration:none"):
        with ui.element("div").classes("tp-logo"):
            ui.icon("explore", size="19px")
        ui.html('<span class="tp-wordmark"' + (' style="color:#fff"' if dark else "") + '>TenderPilot <span class="ai"' + (' style="color:#93C5FD"' if dark else "")
                + '>AI</span></span>')


def _nav_bar() -> None:
    with ui.element("div").classes("tp-public-nav w-full"):
        with ui.row().classes("w-full items-center justify-between no-wrap").style("max-width:1200px;margin:0 auto;padding:14px 24px"):
            _logo()
            with ui.row().classes("items-center gap-6 gt-sm"):
                for label, href in (("Features", "#features"), ("How it works", "#how"), ("Pricing", "#pricing"), ("FAQ", "#faq")):
                    ui.html(f'<a class="navlink" href="{href}">{label}</a>')
            with ui.row().classes("items-center gap-2 no-wrap"):
                ui.button("Sign in", on_click=lambda: ui.navigate.to("/login")).props("flat no-caps").classes("gt-xs")
                ui.button("Start free trial", on_click=lambda: ui.navigate.to("/register")).props("unelevated color=primary")


def _section(id_: str, eyebrow: str, title: str, sub: str | None = None, *, bg: str = "#fff"):
    outer = ui.element("section").classes("w-full").style(f"background:{bg};padding:88px 24px").props(f'id="{id_}"')
    with outer:
        inner = ui.column().classes("w-full items-center gap-3").style("max-width:1200px;margin:0 auto")
        with inner:
            ui.label(eyebrow).classes("tp-eyebrow")
            ui.label(title).classes("tp-section-title")
            if sub:
                ui.label(sub).classes("tp-section-sub")
    return inner


def _preview_card() -> None:
    with ui.element("div").classes("tp-preview w-full").style("max-width:560px"):
        with ui.element("div").classes("tp-preview-bar"):
            ui.html("<i></i><i></i><i></i>")
            ui.label("Eligibility assessment · Street lighting RFP").classes("tp-caption q-ml-sm")
        with ui.column().classes("w-full gap-3").style("padding:20px"):
            with ui.row().classes("w-full items-center no-wrap gap-4"):
                score_ring(72, large=True)
                with ui.column().classes("gap-1"):
                    ui.badge("Gaps found", color="warning").classes("tp-chip")
                    ui.label("2 conditions need action before you bid").classes("text-body2").style("color:#101828;font-weight:550")
                    ui.label("Illustrative example").classes("tp-caption")
            ui.element("div").classes("tp-divider")
            for code, title, label, color in [("R-001", "Avg. turnover ≥ ₹5 Cr (3 FYs)", "Conflicting evidence", "orange"),
                                              ("R-002", "3 years of similar experience", "Verified satisfied", "positive"),
                                              ("R-004", "Valid ISO 9001 certificate", "Not satisfied — expired", "negative"),
                                              ("R-008", "EMD exemption for MSEs", "Needs review", "info")]:
                with ui.row().classes("w-full items-center justify-between no-wrap gap-3"):
                    with ui.row().classes("items-center no-wrap gap-2 col").style("min-width:0"):
                        ui.label(code).style("font-weight:650;color:#475467;font-size:.72rem;background:#F2F4F7;border-radius:6px;padding:1px 6px")
                        ui.label(title).classes("text-body2 ellipsis").style("color:#101828")
                    ui.badge(label, color=color).classes("tp-chip")
            with ui.row().classes("items-center gap-2 q-mt-xs").style("background:#F8FAFF;border:1px solid #D1E0FF;border-radius:10px;padding:10px 12px"):
                ui.icon("format_quote", size="18px").style("color:#2563EB")
                ui.label("Each finding links to the exact page of your own document.").classes("text-caption").style("color:#1D4ED8;font-weight:550")


@ui.page("/")
def landing():
    apply_theme()
    ui.query("body").classes("tp-public")
    _nav_bar()
    with ui.element("section").classes("w-full tp-hero").style("padding:72px 24px 96px"):
        with ui.row().classes("w-full items-center gap-12").style("max-width:1200px;margin:0 auto"):
            with ui.column().classes("col gap-5").style("min-width:320px"):
                ui.html('<span class="tp-pill"><span class="tag">New</span>Tender intelligence for Indian suppliers &amp; contractors</span>')
                ui.html('<div class="tp-hero-title">Discover smarter.<br>Qualify faster.<br><span class="tp-gradient-text">Bid with confidence.</span></div>')
                ui.label("Upload your qualifications once. TenderPilot reads every tender, checks each eligibility condition against your own "
                         "documents with page-level evidence, and prepares a reviewable bid package.").classes("tp-lead")
                with ui.row().classes("gap-3 q-mt-sm"):
                    ui.button("Start free trial", on_click=lambda: ui.navigate.to("/register")).props("unelevated color=primary size=lg icon-right=arrow_forward")
                    ui.button("Sign in", on_click=lambda: ui.navigate.to("/login")).props("outline color=primary size=lg")
                with ui.row().classes("items-center gap-5 q-mt-sm tp-caption"):
                    for icon, text in (("check_circle", "14-day Professional trial"), ("lock", "Tenant-isolated data"), ("verified", "Evidence for every finding")):
                        with ui.row().classes("items-center gap-1 no-wrap"):
                            ui.icon(icon, size="16px").style("color:#079455")
                            ui.label(text)
            with ui.column().classes("col items-center").style("min-width:320px"):
                _preview_card()

    with ui.element("section").classes("w-full").style("border-top:1px solid #EAECF0;border-bottom:1px solid #EAECF0;padding:28px 24px;background:#FCFCFD"):
        with ui.row().classes("w-full items-center justify-around gap-6").style("max-width:1100px;margin:0 auto"):
            for value, label in (("Page-level", "evidence citations"), ("Exact", "rule arithmetic, never guessed"), ("PDF · DOCX · XLSX · ZIP", "tender formats"), ("Full", "audit trail & versioning")):
                with ui.column().classes("items-center gap-0"):
                    ui.label(value).style("font-weight:700;font-size:1.05rem;color:#101828;letter-spacing:-.01em")
                    ui.label(label).classes("tp-caption")

    with _section("features", "Features", "Built for the way tenders actually work",
                  "From the first NIT download to an approved, auditable package — one workspace for your whole bid team."):
        tones = ["primary", "violet", "sky", "green", "amber", "rose"]
        with ui.element("div").classes("tp-grid-3 w-full q-mt-lg"):
            for (icon, title, body), tone in zip(FEATURES, tones, strict=False):
                with ui.card().classes("tp-card tp-panel tp-hover").props("flat").style("padding:24px"):
                    ui.icon(icon).classes(f"tp-feature-icon tp-tone-{tone}")
                    ui.label(title).classes("tp-h2 q-mt-xs").style("font-size:1.02rem")
                    ui.label(body).classes("tp-muted text-body2")

    with _section("how", "How it works", "From tender to bid in four steps", bg="#F9FAFB"):
        with ui.element("div").classes("w-full q-mt-lg").style("display:grid;grid-template-columns:repeat(auto-fit,minmax(min(230px,100%),1fr));gap:16px"):
            for i, (icon, title, body) in enumerate(STEPS, 1):
                with ui.card().classes("tp-card tp-panel").props("flat").style("padding:24px"):
                    with ui.row().classes("w-full items-center justify-between"):
                        ui.label(f"{i}").classes("tp-step-num")
                        ui.icon(icon, size="22px").style("color:#98A2B3")
                    ui.label(title).classes("tp-h2 q-mt-sm").style("font-size:1.02rem")
                    ui.label(body).classes("tp-muted text-body2")

    with _section("pricing", "Pricing", "Simple plans that scale with your bid volume", "14-day trial on Professional. Prices exclude GST."):
        with ui.row().classes("w-full gap-5 q-mt-lg justify-center items-stretch"):
            for p in PLAN_DEFINITIONS:
                featured = p.get("code") == "professional" or p["name"].lower().startswith("prof")
                with ui.card().classes("tp-card tp-panel col tp-plan" + (" featured" if featured else "")).props("flat").style("max-width:360px;min-width:260px;padding:28px"):
                    with ui.row().classes("w-full items-center justify-between"):
                        ui.label(p["name"]).classes("tp-h2").style("font-size:1.05rem")
                        if featured:
                            ui.badge("Most popular", color="primary").classes("tp-chip")
                    with ui.row().classes("items-baseline gap-1"):
                        ui.label(f"₹{p['price_inr_monthly']:,.0f}" if p["price_inr_monthly"] else "Custom").style("font-size:2.2rem;font-weight:700;letter-spacing:-.03em;color:#101828")
                        if p["price_inr_monthly"]:
                            ui.label("/month").classes("tp-muted")
                    ui.element("div").classes("tp-divider q-my-xs")
                    lim = p["limits"]
                    for line in (f"{lim['members']} team members", f"{lim['tenders_analysed_per_month']} tender analyses / month",
                                 f"{lim['documents_processed_per_month']} documents / month", f"{lim['storage_bytes'] // 1024**3} GB storage"):
                        with ui.row().classes("items-center no-wrap gap-2"):
                            ui.icon("check", size="18px").style("color:#2563EB")
                            ui.label(line).classes("text-body2")
                    ui.element("div").classes("col")
                    ui.button("Contact sales" if not p["price_inr_monthly"] else "Start free trial", on_click=lambda: ui.navigate.to("/register")).props(
                        ("unelevated" if featured else "outline") + " color=primary").classes("w-full q-mt-md")

    with _section("faq", "FAQ", "Frequently asked questions", bg="#F9FAFB"):
        with ui.column().classes("w-full gap-3 q-mt-md").style("max-width:780px"):
            for q, a in FAQ:
                with ui.expansion(q).classes("tp-card w-full").props("expand-icon-toggle=false"):
                    ui.label(a).classes("tp-muted text-body2").style("padding:0 16px 14px")

    with ui.element("section").classes("w-full").style("padding:72px 24px"):
        with ui.column().classes("w-full items-center gap-4 tp-cta-band text-center").style("max-width:1100px;margin:0 auto;padding:56px 24px"):
            ui.label("Stop guessing whether you qualify.").style("font-size:clamp(1.5rem,3vw,2.2rem);font-weight:700;letter-spacing:-.03em;color:#fff")
            ui.label("Bring one tender and your certificates — see a cited eligibility assessment in minutes.").style("color:#C7D2FE;font-size:1.02rem")
            with ui.row().classes("gap-3 q-mt-sm justify-center"):
                ui.button("Start free trial", on_click=lambda: ui.navigate.to("/register")).props("unelevated size=lg").style("background:#fff!important;color:#1E3A8A!important")
                ui.button("Sign in", on_click=lambda: ui.navigate.to("/login")).props("outline size=lg").style("color:#fff!important")

    with ui.element("footer").classes("w-full tp-footer").style("padding:28px 24px"):
        with ui.row().classes("w-full items-center justify-between gap-4").style("max-width:1200px;margin:0 auto"):
            _logo()
            ui.label("© TenderPilot AI · Not affiliated with any government procurement portal · Not legal advice").classes("tp-caption")


AUTH_POINTS = [("Every eligibility condition checked against your own documents", "fact_check"),
               ("Page-level citations you can open and verify", "format_quote"),
               ("Deterministic rules for turnover, dates and thresholds", "rule"),
               ("Your data isolated per organization, enforced server-side", "lock")]


def _auth_card(title: str, subtitle: str):
    apply_theme()
    ui.query("body").style("background:#fff")
    with ui.row().classes("w-full no-wrap items-stretch").style("min-height:100vh;gap:0;background:#fff"):
        with ui.column().classes("tp-auth-aside gt-sm justify-between").style("width:46%;max-width:620px;padding:40px 48px"):
            _logo(dark=True)
            with ui.column().classes("gap-5"):
                ui.html('<div style="font-size:2.1rem;line-height:1.15;font-weight:700;letter-spacing:-.03em">Win the tenders you<br>'
                        '<span style="background:linear-gradient(90deg,#93C5FD,#C4B5FD);-webkit-background-clip:text;background-clip:text;color:transparent">'
                        'actually qualify for.</span></div>')
                with ui.column().classes("gap-3"):
                    for text, icon in AUTH_POINTS:
                        with ui.row().classes("items-center no-wrap gap-3"):
                            with ui.element("div").classes("tp-check"):
                                ui.icon(icon, size="14px")
                            ui.label(text).style("color:#D0D5DD;font-size:.92rem")
            ui.label("© TenderPilot AI · Not affiliated with any government procurement portal").classes("muted text-caption")
        with ui.column().classes("col items-center justify-center").style("padding:40px 24px"):
            with ui.column().classes("lt-md q-mb-lg"):
                _logo()
            card = ui.column().classes("w-full gap-4").style("max-width:400px")
            with card:
                with ui.column().classes("gap-1 q-mb-sm"):
                    ui.label(title).classes("tp-h1").style("font-size:1.75rem")
                    if subtitle:
                        ui.label(subtitle).classes("tp-muted")
        return card


def _error_banner(request: Request) -> None:
    err = request.query_params.get("error")
    if err:
        with ui.row().classes("w-full items-center no-wrap gap-2").style("background:#FEF3F2;border:1px solid #FECDCA;border-radius:10px;padding:10px 12px"):
            ui.icon("error_outline", size="18px").style("color:#D92D20")
            ui.label("Request blocked: please submit the form from this site." if err == "origin" else err).classes("text-body2").style("color:#B42318")


@ui.page("/login")
def login(request: Request):
    with _auth_card("Sign in", "Welcome back to TenderPilot AI."):
        _error_banner(request)
        with ui.element("form").props('method="post" action="/auth/form/login"').classes("w-full column gap-3"):
            ui.input("Email").props('name=email type=email outlined autocomplete=email autofocus').classes("w-full")
            ui.input("Password", password=True, password_toggle_button=True).props('name=password outlined autocomplete=current-password').classes("w-full")
            ui.html(f'<input type="hidden" name="next" value="{html.escape(request.query_params.get("next", "/dashboard"), quote=True)}">', sanitize=False)
            with ui.row().classes("w-full justify-end").style("margin-top:-4px"):
                ui.link("Forgot password?", "/forgot-password").classes("tp-link text-caption")
            ui.button("Sign in").props("type=submit unelevated color=primary size=md").classes("w-full").style("min-height:44px")
        with ui.row().classes("w-full justify-center gap-1 q-mt-sm"):
            ui.label("New to TenderPilot?").classes("tp-muted")
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
            ui.button("Create account").props("type=submit unelevated color=primary").classes("w-full").style("min-height:44px")
            ui.label("By creating an account you agree to use TenderPilot for preparing your own organization's bids.").classes("tp-caption text-center")
        with ui.row().classes("w-full justify-center gap-1"):
            ui.label("Already have an account?").classes("tp-muted")
            ui.link("Sign in", "/login").classes("tp-link")


@ui.page("/forgot-password")
def forgot():
    with _auth_card("Reset your password", "We'll email you a link if an account exists."):
        email = ui.input("Email").props("outlined type=email").classes("w-full")

        def submit():
            with session_scope() as db:
                auth_service.request_password_reset(db, email.value or "")
            ui.notify("If an account exists for that email, a reset link has been sent.", type="info")

        ui.button("Send reset link", on_click=submit).props("unelevated color=primary").classes("w-full").style("min-height:44px")
        with ui.row().classes("w-full justify-center"):
            ui.link("Back to sign in", "/login").classes("tp-link")


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

        ui.button("Update password", on_click=submit).props("unelevated color=primary").classes("w-full").style("min-height:44px")


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

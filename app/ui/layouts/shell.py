"""Authenticated application shell and per-page context helpers."""

from __future__ import annotations

import html
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import quote

from fastapi import Request
from nicegui import ui

from app.auth.context import TenantContext
from app.auth.deps import ui_principal
from app.auth.permissions import ROLE_LABELS, Perm
from app.core.config import get_settings
from app.core.errors import AppError
from app.db import tenancy
from app.db.models import Organization
from app.db.session import session_scope
from app.services.notifications import service as notifications
from app.services.organizations import auth_service, org_service
from app.ui.themes.theme import apply_theme, initials

NAV = [
    ("", [("Dashboard", "space_dashboard", "/dashboard")]),
    ("Pipeline", [("Opportunities", "travel_explore", "/opportunities"), ("My Tenders", "description", "/tenders"),
                  ("Bid Workspaces", "workspaces", "/bids")]),
    ("Intelligence", [("Eligibility", "fact_check", "/eligibility"), ("Evidence Graph", "hub", "/evidence"),
                      ("AI Copilot", "auto_awesome", "/copilot"), ("Analytics", "insights", "/analytics")]),
    ("Company", [("Document Vault", "folder_shared", "/vault"), ("Company Profile", "apartment", "/company")]),
    ("Workspace", [("Team", "groups", "/team"), ("Settings", "settings", "/settings")]),
]


@dataclass
class PageCtx:
    ctx: TenantContext
    user_name: str
    org_name: str
    is_demo: bool
    session_token: str | None

    def can(self, perm: Perm) -> bool:
        return self.ctx.can(perm)


def _client_tenant() -> uuid.UUID | None:
    try:
        from nicegui import context

        return getattr(context.client, "tp_org_id", None)
    except (RuntimeError, AttributeError, IndexError):  # not inside a NiceGUI client context
        return None


tenancy.register_fallback(_client_tenant)


def require_ctx(request: Request) -> PageCtx | None:
    with session_scope() as db:
        p = ui_principal(request, db)
        if p is None:
            ui.navigate.to(f"/login?next={request.url.path}")
            return None
        if p.ctx is None:
            ui.navigate.to("/onboarding")
            return None
        org = db.get(Organization, p.ctx.organization_id)
        # Later UI events (clicks, timers) run in other tasks; they find the tenant through this client.
        ui.context.client.tp_org_id = p.ctx.organization_id  # type: ignore[attr-defined]
        return PageCtx(p.ctx, p.user.full_name, org.name if org else "", bool(org and org.is_demo), request.cookies.get(get_settings().session_cookie_name))


def run(fn: Callable, *, success: str | None = None, refresh: Callable | None = None):
    """Execute a UI action against the service layer inside one transaction, with user-facing errors."""
    try:
        with session_scope() as db:
            result = fn(db)
        if success:
            ui.notify(success, type="positive")
        if refresh:
            refresh()
        return result
    except AppError as exc:
        ui.notify(exc.message, type="negative" if exc.status_code >= 403 else "warning", multi_line=True)
    except Exception as exc:  # unexpected: never leak internals, but tell the user something failed
        from app.core.logging import get_logger

        get_logger("ui").error("ui.action_failed", error=str(exc))
        ui.notify("Something went wrong. The action was not completed.", type="negative")
    return None


@contextmanager
def shell(request: Request, active: str):
    pc = require_ctx(request)
    if pc is None:
        yield None
        return
    apply_theme()
    with session_scope() as db:
        orgs = org_service.list_user_organizations(db, pc.ctx.user_id)
        unread = notifications.unread_count(db, pc.ctx)
        recent = notifications.list_for_user(db, pc.ctx, limit=8)
        recent_rows = [(n.title, n.link, n.read_at is None, n.created_at) for n in recent]
    page_label = next((label for _s, items in NAV for label, _i, href in items if href == active), "Search" if active == "" else "")
    role_label = ROLE_LABELS.get(pc.ctx.role, pc.ctx.role)

    # Cmd/Ctrl+K focuses the global search.
    ui.add_body_html("""<script>document.addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k'){
      const i=document.querySelector('.tp-search input'); if(i){e.preventDefault(); i.focus(); i.select();}}});</script>""")

    with ui.header().classes("tp-header items-center justify-between no-wrap").style("padding:0 20px 0 24px"):
        with ui.row().classes("items-center no-wrap gap-3"):
            ui.button(icon="menu", on_click=lambda: drawer.toggle()).props("flat round dense").classes("lt-md tp-icon-btn")
            with ui.row().classes("items-center no-wrap gap-2 tp-crumb gt-xs"):
                ui.label(pc.org_name).classes("ellipsis").style("max-width:240px")
                if page_label:
                    ui.icon("chevron_right", size="16px").style("color:#D0D5DD")
                    ui.html(f"<b>{html.escape(page_label)}</b>")
        with ui.row().classes("items-center no-wrap gap-2"):
            search = ui.input(placeholder="Search tenders, documents…").props("dense outlined clearable").classes("tp-search gt-sm").style("width: 340px")
            with search.add_slot("prepend"):
                ui.icon("search", size="18px").style("color:#98A2B3")
            with search.add_slot("append"):
                ui.html('<span class="tp-kbd">⌘K</span>')
            search.on("keydown.enter", lambda: ui.navigate.to(f"/search?q={quote(search.value or '')}"))
            ui.button(icon="search", on_click=lambda: ui.navigate.to("/search")).props("flat round dense").classes("lt-md tp-icon-btn")
            if pc.is_demo:
                ui.badge("Demo data", color="orange").classes("tp-chip").tooltip("This organization contains clearly-labelled synthetic sample data.")
            with ui.button(icon="notifications_none").props("flat round").classes("tp-icon-btn").tooltip("Notifications"):
                if unread:
                    ui.badge(str(unread)).props("floating rounded")
                with ui.menu().props("anchor='bottom right' self='top right' :offset='[0, 8]'").style("width: 380px"):
                    with ui.row().classes("w-full items-center justify-between q-px-md q-pt-md q-pb-sm no-wrap"):
                        ui.label("Notifications").classes("tp-h2")
                        if unread:
                            ui.badge(f"{unread} new", color="primary").classes("tp-chip")
                    if not recent_rows:
                        with ui.column().classes("items-center w-full q-pa-lg gap-1"):
                            ui.icon("notifications_off", size="28px").style("color:#D0D5DD")
                            ui.label("You're all caught up.").classes("tp-muted")
                    for title, link, is_unread, at in recent_rows:
                        with ui.item(on_click=(lambda link=link: ui.navigate.to(link)) if link else None):
                            with ui.item_section().props("side").style("padding-right:10px"):
                                ui.element("span").classes("tp-dot").style(f"background:{'#2563EB' if is_unread else '#EAECF0'}")
                            with ui.item_section():
                                ui.item_label(title).classes("text-weight-medium" if is_unread else "").style("color:#101828" if is_unread else "")
                                ui.item_label(at.strftime("%d %b, %H:%M")).props("caption")
                    ui.separator().classes("q-mt-xs")
                    with ui.row().classes("w-full justify-end q-pa-sm"):
                        ui.button("Mark all as read", icon="done_all", on_click=lambda: run(lambda db: notifications.mark_read(db, pc.ctx), success="Marked as read")).props("flat dense no-caps color=primary")
            ui.element("div").style("width:1px;height:24px;background:#EAECF0;margin:0 4px")
            with ui.button().props("flat no-caps dense").classes("q-px-xs"):
                with ui.row().classes("items-center no-wrap gap-2"):
                    ui.html(f'<div class="tp-avatar">{html.escape(initials(pc.user_name))}</div>')
                    with ui.column().classes("gap-0 items-start gt-sm").style("line-height:1.1"):
                        ui.label(pc.user_name).classes("text-weight-medium").style("color:#101828;font-size:.85rem")
                        ui.label(role_label).style("color:#667085;font-size:.72rem")
                    ui.icon("expand_more", size="18px").classes("gt-sm").style("color:#98A2B3")
                with ui.menu().props("anchor='bottom right' self='top right' :offset='[0, 8]'").style("min-width: 240px"):
                    with ui.row().classes("items-center no-wrap gap-3 q-pa-md"):
                        ui.html(f'<div class="tp-avatar">{html.escape(initials(pc.user_name))}</div>')
                        with ui.column().classes("gap-0"):
                            ui.label(pc.user_name).classes("text-weight-medium").style("color:#101828")
                            ui.label(f"{role_label} · {pc.org_name}").classes("tp-caption ellipsis").style("max-width:170px")
                    ui.separator()
                    for label, icon, href in (("Company profile", "apartment", "/company"), ("Team", "group", "/team"), ("Settings", "settings", "/settings")):
                        with ui.item(on_click=lambda href=href: ui.navigate.to(href)):
                            with ui.item_section().props("avatar").style("min-width:30px"):
                                ui.icon(icon, size="18px").style("color:#667085")
                            ui.item_section(label)
                    ui.separator()
                    with ui.item(on_click=lambda: ui.navigate.to("/auth/form/logout")):
                        with ui.item_section().props("avatar").style("min-width:30px"):
                            ui.icon("logout", size="18px").style("color:#D92D20")
                        ui.item_section("Sign out").style("color:#B42318")

    with ui.left_drawer().props("width=264 breakpoint=1023").classes("tp-drawer") as drawer:
        with ui.element("a").props('href="/dashboard"').classes("row items-center no-wrap gap-2").style("padding:18px 18px 14px;text-decoration:none"):
            with ui.element("div").classes("tp-logo"):
                ui.icon("explore", size="19px")
            ui.html('<span class="tp-wordmark">TenderPilot <span class="ai">AI</span></span>')
        org_options = {str(o.id): o.name for o, _ in orgs}

        def switch(e):
            if e.value and e.value != str(pc.ctx.organization_id):
                def do(db):
                    from sqlalchemy import select

                    from app.core.security import hash_token
                    from app.db.models import UserSession

                    sess = db.scalar(select(UserSession).where(UserSession.token_hash == hash_token(pc.session_token or "")))
                    auth_service.switch_organization(db, sess, uuid.UUID(e.value))
                run(do)
                ui.navigate.to("/dashboard")

        with ui.element("div").style("padding:0 14px"):
            org = ui.select(org_options, value=str(pc.ctx.organization_id), on_change=switch).props("dense outlined options-dense").classes("w-full tp-org-switch")
            with org.add_slot("prepend"):
                ui.html(f'<div class="tp-avatar sm" style="border-radius:7px;background:#101828;color:#fff">{html.escape(initials(pc.org_name))}</div>')
        with ui.element("nav").classes("col").style("padding:2px 12px 8px;overflow-y:auto").props('aria-label="Main navigation"'):
            for section, items in NAV:
                if section:
                    ui.label(section).classes("tp-nav-section")
                else:
                    ui.element("div").style("height:12px")
                for label, icon, href in items:
                    is_active = active == href
                    with ui.element("a").props(f'href="{href}"' + (' aria-current="page"' if is_active else "")).classes("tp-nav-item" + (" active" if is_active else "")):
                        ui.icon(icon)
                        ui.label(label)
        llm = get_settings().llm_configured
        with ui.element("div").classes("tp-status-card"):
            with ui.row().classes("items-center no-wrap gap-2"):
                ui.element("span").classes("tp-dot").style(f"background:{'#12B76A' if llm else '#F79009'};box-shadow:0 0 0 3px {'#D1FADF' if llm else '#FEF0C7'}")
                ui.label("AI provider connected" if llm else "Rule-based demo mode").classes("text-weight-medium").style("font-size:.8rem;color:#101828")
            ui.label("Extraction and copilot use your configured model." if llm else "No LLM key configured — deterministic, labelled results.").classes("tp-caption q-mt-xs")

    with ui.column().classes("tp-main w-full"):
        yield pc

"""Authenticated application shell and per-page context helpers."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass

from fastapi import Request
from nicegui import ui

from app.auth.context import TenantContext
from app.auth.deps import ui_principal
from app.auth.permissions import ROLE_LABELS, Perm
from app.core.config import get_settings
from app.core.errors import AppError
from app.db.models import Organization
from app.db.session import session_scope
from app.services.notifications import service as notifications
from app.services.organizations import auth_service, org_service
from app.ui.themes.theme import NAVY, apply_theme

NAV = [
    ("Dashboard", "space_dashboard", "/dashboard"),
    ("Opportunities", "travel_explore", "/opportunities"),
    ("My Tenders", "description", "/tenders"),
    ("Bid Workspaces", "workspaces", "/bids"),
    ("Eligibility Intelligence", "fact_check", "/eligibility"),
    ("Evidence Graph", "hub", "/evidence"),
    ("Document Vault", "folder_shared", "/vault"),
    ("Company Profile", "apartment", "/company"),
    ("AI Copilot", "auto_awesome", "/copilot"),
    ("Analytics", "insights", "/analytics"),
    ("Team", "groups", "/team"),
    ("Settings", "settings", "/settings"),
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

    with ui.header().classes("items-center justify-between q-px-md").style(f"background:#fff;color:{NAVY};border-bottom:1px solid #E3E8EF;height:60px"):
        with ui.row().classes("items-center no-wrap gap-2"):
            ui.button(icon="menu", on_click=lambda: drawer.toggle()).props("flat round dense color=primary").classes("lt-md")
            search = ui.input(placeholder="Search tenders, documents…").props("dense outlined rounded clearable").classes("gt-xs").style("width: 340px")
            search.on("keydown.enter", lambda: ui.navigate.to(f"/search?q={search.value or ''}"))
        with ui.row().classes("items-center no-wrap gap-1"):
            if pc.is_demo:
                ui.badge("DEMO DATA", color="orange").props("outline").tooltip("This organization contains clearly-labelled synthetic sample data.")
            with ui.button(icon="notifications").props("flat round color=primary").tooltip("Notifications"):
                if unread:
                    ui.badge(str(unread), color="negative").props("floating rounded")
                with ui.menu().classes("q-pa-sm").style("width: 360px"):
                    ui.label("Notifications").classes("tp-h2 q-pa-sm")
                    if not recent_rows:
                        ui.label("You're all caught up.").classes("tp-muted q-pa-sm")
                    for title, link, is_unread, at in recent_rows:
                        with ui.item(on_click=(lambda link=link: ui.navigate.to(link)) if link else None):
                            with ui.item_section():
                                ui.item_label(title).classes("text-weight-medium" if is_unread else "")
                                ui.item_label(at.strftime("%d %b %H:%M")).props("caption")
                    ui.separator()
                    ui.button("Mark all read", on_click=lambda: run(lambda db: notifications.mark_read(db, pc.ctx), success="Marked as read")).props("flat dense")
            with ui.button(icon="account_circle").props("flat round color=primary"):
                with ui.menu():
                    ui.item(pc.user_name).props("dense").classes("text-weight-medium")
                    ui.item(ROLE_LABELS.get(pc.ctx.role, pc.ctx.role)).props("dense").classes("tp-muted")
                    ui.separator()
                    ui.menu_item("Settings", on_click=lambda: ui.navigate.to("/settings"))
                    ui.menu_item("Sign out", on_click=lambda: ui.navigate.to("/auth/form/logout"))

    with ui.left_drawer(value=True, bordered=False).props("width=252 breakpoint=1023").style(f"background:{NAVY}") as drawer:
        with ui.row().classes("items-center q-px-md q-pt-md q-pb-sm no-wrap"):
            ui.icon("explore", size="26px").classes("text-secondary")
            ui.label("TenderPilot AI").classes("text-white text-weight-bold text-subtitle1")
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

        ui.select(org_options, value=str(pc.ctx.organization_id), on_change=switch).props("dense dark outlined options-dense").classes("q-mx-md q-mb-md")
        with ui.list().classes("tp-nav"):
            for label, icon, href in NAV:
                with ui.item(on_click=lambda href=href: ui.navigate.to(href)).classes("tp-active" if active == href else ""):
                    with ui.item_section().props("avatar").style("min-width: 34px"):
                        ui.icon(icon, size="20px")
                    with ui.item_section():
                        ui.item_label(label)
        with ui.column().classes("absolute-bottom q-pa-md"):
            mode = "Rule-based demo mode" if not get_settings().llm_configured else "AI provider connected"
            ui.label(mode).classes("text-caption").style("color:#8EA0BC")

    with ui.column().classes("w-full q-pa-lg gap-4").style("max-width: 1400px; margin: 0 auto"):
        yield pc

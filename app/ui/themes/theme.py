"""Visual identity: deep navy / slate / white with restrained teal accents; semantic status colours."""

from __future__ import annotations

from nicegui import ui

NAVY = "#14213D"
NAVY_2 = "#1F2A44"
SLATE = "#5B6B7F"
TEAL = "#0F9D8F"
BG = "#F5F7FA"

STATUS = {
    "verified_satisfied": ("Verified satisfied", "positive", "check_circle"),
    "verified_not_satisfied": ("Not satisfied", "negative", "cancel"),
    "insufficient_evidence": ("Insufficient evidence", "warning", "help"),
    "conflicting_evidence": ("Conflicting evidence", "orange", "report"),
    "needs_human_review": ("Needs review", "info", "rate_review"),
    "not_applicable": ("Not applicable", "grey-6", "remove_circle_outline"),
}
OVERALL = {
    "eligible_per_policy": ("Eligible per policy", "positive"),
    "needs_review": ("Needs review", "info"),
    "gaps_found": ("Gaps found", "warning"),
    "not_eligible": ("Not eligible on current records", "negative"),
    "no_requirements": ("No requirements", "grey-6"),
}
JOB_COLOR = {"queued": "grey-6", "running": "info", "retrying": "warning", "succeeded": "positive", "failed": "negative", "cancelled": "grey-6"}

CSS = f"""
:root {{ --tp-navy: {NAVY}; --tp-teal: {TEAL}; --tp-slate: {SLATE}; }}
body {{ background: {BG}; font-family: 'Inter', system-ui, -apple-system, 'Segoe UI', sans-serif; color: #1B2333; }}
.tp-card {{ background: #fff; border: 1px solid #E3E8EF; border-radius: 12px; box-shadow: 0 1px 2px rgba(20,33,61,.04); }}
.tp-kpi .value {{ font-size: 1.65rem; font-weight: 650; color: {NAVY}; letter-spacing: -0.02em; }}
.tp-kpi .label {{ font-size: .78rem; color: {SLATE}; text-transform: uppercase; letter-spacing: .06em; }}
.tp-muted {{ color: {SLATE}; }}
.tp-h1 {{ font-size: 1.45rem; font-weight: 650; color: {NAVY}; letter-spacing: -0.01em; }}
.tp-h2 {{ font-size: 1.05rem; font-weight: 600; color: {NAVY}; }}
.tp-nav .q-item {{ border-radius: 8px; margin: 2px 8px; color: #C9D3E3; min-height: 38px; }}
.tp-nav .q-item.tp-active {{ background: rgba(15,157,143,.18); color: #fff; }}
.tp-nav .q-item:hover {{ background: rgba(255,255,255,.06); }}
.tp-quote {{ border-left: 3px solid {TEAL}; background: #F2FAF9; padding: 8px 12px; border-radius: 6px; font-size: .88rem; white-space: pre-wrap; }}
.tp-hl {{ position: absolute; border: 2px solid {TEAL}; background: rgba(15,157,143,.15); border-radius: 3px; pointer-events: none; }}
.tp-landing-hero {{ background: radial-gradient(1200px 500px at 70% -10%, rgba(15,157,143,.25), transparent), linear-gradient(160deg, {NAVY} 0%, #0B1528 100%); color: #fff; }}
.tp-chip {{ font-size: .72rem; }}
a.tp-link {{ color: {TEAL}; text-decoration: none; font-weight: 500; }}
a.tp-link:hover {{ text-decoration: underline; }}
.q-table th {{ font-weight: 600; color: {SLATE}; text-transform: uppercase; font-size: .72rem; letter-spacing: .05em; }}
:focus-visible {{ outline: 2px solid {TEAL}; outline-offset: 2px; }}
"""


def apply_theme() -> None:
    ui.colors(primary=NAVY, secondary=TEAL, accent=TEAL, positive="#1E8E5A", negative="#C0392B", warning="#D4A017", info="#2E86C1")
    ui.add_head_html(f"<style>{CSS}</style>")
    ui.add_head_html('<link rel="preconnect" href="https://fonts.googleapis.com"><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">')


def status_badge(status: str | None) -> None:
    label, color, icon = STATUS.get(status or "", ((status or "—").replace("_", " "), "grey-6", "circle"))
    with ui.badge(color=color).classes("tp-chip q-px-sm q-py-xs").props("rounded"):
        ui.icon(icon).classes("q-mr-xs")
        ui.label(label)


def overall_badge(status: str | None) -> None:
    label, color = OVERALL.get(status or "", ((status or "Not assessed").replace("_", " ").capitalize(), "grey-6"))
    ui.badge(label, color=color).classes("tp-chip q-px-sm q-py-xs").props("rounded")


def kpi(label: str, value: str, icon: str, hint: str | None = None, link: str | None = None) -> None:
    with ui.card().classes("tp-card tp-kpi q-pa-md col").style("min-width: 180px; cursor: pointer" if link else "min-width: 180px") as c:
        with ui.row().classes("items-center justify-between w-full no-wrap"):
            ui.label(label).classes("label")
            ui.icon(icon, size="20px").classes("text-secondary")
        ui.label(value).classes("value")
        if hint:
            ui.label(hint).classes("text-caption tp-muted")
    if link:
        c.on("click", lambda: ui.navigate.to(link))


def empty_state(icon: str, title: str, body: str, action_label: str | None = None, on_action=None) -> None:
    with ui.column().classes("items-center q-pa-xl w-full text-center"):
        ui.icon(icon, size="48px").classes("text-grey-5")
        ui.label(title).classes("tp-h2")
        ui.label(body).classes("tp-muted").style("max-width: 480px")
        if action_label and on_action:
            ui.button(action_label, on_click=on_action).props("unelevated color=secondary")

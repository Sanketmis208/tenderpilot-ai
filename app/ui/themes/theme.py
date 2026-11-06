"""Visual identity and shared presentational helpers.

One design system for every page: neutral surfaces, a single confident brand blue, soft semantic status
pills, consistent page headers, panels, KPI tiles and empty states. Quasar components are restyled
globally here so individual pages stay focused on content.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from nicegui import ui

# ---- design tokens -------------------------------------------------------------------------------------------
BRAND = "#2563EB"        # primary actions, links, focus
BRAND_DARK = "#1D4ED8"
BRAND_SOFT = "#EFF4FF"
INK = "#101828"          # headings
TEXT = "#344054"         # body copy
MUTED = "#667085"        # secondary copy
BORDER = "#EAECF0"
BORDER_STRONG = "#D0D5DD"
BG = "#F7F8FA"
SURFACE = "#FFFFFF"

# Kept for backwards compatibility with modules importing the old names.
NAVY = INK
NAVY_2 = TEXT
SLATE = MUTED
TEAL = BRAND

POSITIVE, NEGATIVE, WARNING, INFO, ORANGE = "#079455", "#D92D20", "#DC6803", "#1570EF", "#E04F16"
CHART_COLORS = [BRAND, "#7C3AED", "#0EA5E9", "#14B8A6", "#F59E0B", "#F43F5E", "#64748B"]

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

# Soft badge palette: (background, text, ring) per Quasar colour name used across pages.
_BADGE = {
    "positive": ("#ECFDF3", "#067647", "#ABEFC6"),
    "negative": ("#FEF3F2", "#B42318", "#FECDCA"),
    "warning": ("#FFFAEB", "#B54708", "#FEDF89"),
    "info": ("#EFF8FF", "#175CD3", "#B2DDFF"),
    "orange": ("#FFF4ED", "#B93815", "#F9DBAF"),
    "primary": ("#EFF4FF", "#1D4ED8", "#C7D7FE"),
    "secondary": ("#EFF4FF", "#1D4ED8", "#C7D7FE"),
    "accent": ("#F4F3FF", "#5925DC", "#D9D6FE"),
    "grey": ("#F2F4F7", "#344054", "#EAECF0"),
    "grey-5": ("#F2F4F7", "#475467", "#EAECF0"),
    "grey-6": ("#F2F4F7", "#475467", "#EAECF0"),
    "grey-7": ("#F2F4F7", "#344054", "#EAECF0"),
}


def _badge_css() -> str:
    out = []
    for name, (bg, fg, ring) in _BADGE.items():
        out.append(f".q-badge.bg-{name}{{background:{bg}!important;color:{fg}!important;box-shadow:inset 0 0 0 1px {ring}}}")
        out.append(f".q-badge--outline.text-{name}{{color:{fg}!important;border-color:{ring}!important;background:#fff}}")
    return "\n".join(out)


CSS = f"""
:root {{
  --tp-brand:{BRAND}; --tp-brand-dark:{BRAND_DARK}; --tp-brand-soft:{BRAND_SOFT};
  --tp-ink:{INK}; --tp-text:{TEXT}; --tp-muted:{MUTED}; --tp-border:{BORDER}; --tp-border-strong:{BORDER_STRONG};
  --tp-bg:{BG}; --tp-surface:{SURFACE};
  --tp-shadow-xs:0 1px 2px rgba(16,24,40,.05);
  --tp-shadow-sm:0 1px 3px rgba(16,24,40,.08),0 1px 2px rgba(16,24,40,.04);
  --tp-shadow-lg:0 12px 32px -8px rgba(16,24,40,.16),0 4px 8px -4px rgba(16,24,40,.06);
  --tp-radius:12px;
}}
html, body {{ background:var(--tp-bg); }}
.nicegui-content {{ padding:0; }}
body {{ font-family:'Inter',system-ui,-apple-system,'Segoe UI',sans-serif; color:var(--tp-text); font-size:14px;
  -webkit-font-smoothing:antialiased; font-feature-settings:'cv11','ss01'; }}
::selection {{ background:#D1E0FF; }}
* {{ scrollbar-width:thin; scrollbar-color:#D0D5DD transparent; }}
:focus-visible {{ outline:2px solid var(--tp-brand); outline-offset:2px; }}
a {{ color:var(--tp-brand); }}

/* ---------- typography ---------- */
.tp-h1 {{ font-size:1.5rem; line-height:2rem; font-weight:650; color:var(--tp-ink); letter-spacing:-.022em; }}
.tp-h2 {{ font-size:.95rem; line-height:1.4rem; font-weight:600; color:var(--tp-ink); letter-spacing:-.005em; }}
.tp-h3 {{ font-size:.875rem; font-weight:600; color:var(--tp-ink); }}
.tp-subtitle {{ color:var(--tp-muted); font-size:.9rem; line-height:1.35rem; max-width:760px; }}
.tp-muted {{ color:var(--tp-muted); }}
.tp-caption {{ color:var(--tp-muted); font-size:.78rem; line-height:1.1rem; }}
.tp-eyebrow {{ font-size:.72rem; font-weight:600; letter-spacing:.08em; text-transform:uppercase; color:var(--tp-brand); }}
.text-caption {{ font-size:.78rem; line-height:1.15rem; letter-spacing:0; }}
.text-body1 {{ font-size:.92rem; line-height:1.4rem; }}
.text-body2 {{ font-size:.875rem; line-height:1.35rem; letter-spacing:0; }}
.text-subtitle1 {{ font-size:.98rem; font-weight:600; letter-spacing:-.005em; }}
.text-subtitle2 {{ font-size:.9rem; font-weight:600; color:var(--tp-ink); letter-spacing:0; }}
.text-weight-medium {{ font-weight:550!important; }}
.tp-num {{ font-variant-numeric:tabular-nums; }}
a.tp-link {{ color:var(--tp-brand); text-decoration:none; font-weight:550; }}
a.tp-link:hover {{ color:var(--tp-brand-dark); text-decoration:underline; text-underline-offset:3px; }}
.ellipsis {{ max-width:100%; }}
.tp-back {{ max-width:100%; display:inline-flex; align-items:center; gap:4px; color:var(--tp-muted); font-size:.8rem; font-weight:500; text-decoration:none; }}
.tp-back:hover {{ color:var(--tp-ink); }}

/* ---------- surfaces ---------- */
.q-card, .tp-card {{ background:var(--tp-surface); border:1px solid var(--tp-border); border-radius:var(--tp-radius); box-shadow:var(--tp-shadow-xs); }}
.tp-card.cursor-pointer, .tp-hover {{ transition:border-color .15s, box-shadow .15s, transform .15s; }}
.tp-card.cursor-pointer:hover, .tp-hover:hover {{ border-color:#C7D7FE; box-shadow:var(--tp-shadow-sm); }}
.tp-panel {{ padding:20px; gap:14px; }}
.tp-panel-head {{ padding-bottom:2px; }}
.tp-panel-icon {{ width:32px; height:32px; border-radius:8px; background:var(--tp-brand-soft); color:var(--tp-brand); font-size:18px; display:flex; align-items:center; justify-content:center; }}
.tp-divider {{ border-top:1px solid var(--tp-border); width:100%; }}
.tp-row-line {{ border-bottom:1px solid #F2F4F7; padding:9px 0; }}
.tp-row-line:last-child {{ border-bottom:none; }}
.tp-quote {{ border-left:3px solid #84ADFF; background:#F8FAFC; padding:10px 14px; border-radius:0 8px 8px 0; font-size:.85rem; line-height:1.4rem; color:var(--tp-text); white-space:pre-wrap; }}
.tp-hl {{ position:absolute; border:2px solid var(--tp-brand); background:rgba(37,99,235,.14); border-radius:4px; pointer-events:none; box-shadow:0 0 0 4px rgba(37,99,235,.12); }}
.tp-chip {{ font-size:.74rem; }}
.tp-dot {{ width:8px; height:8px; border-radius:50%; display:inline-block; flex:none; }}

/* ---------- page layout ---------- */
.tp-main {{ max-width:1320px; margin:0 auto; padding:28px 32px 56px; gap:20px; }}
@media (max-width:700px) {{ .tp-main {{ padding:20px 16px 40px; }} }}
.tp-page-header {{ gap:16px; flex-wrap:wrap; margin-bottom:4px; }}
.tp-kpi-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(min(200px,100%),1fr)); gap:16px; }}
.tp-grid-2 {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(min(380px,100%),1fr)); gap:16px; align-items:start; }}
.tp-grid-3 {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(min(300px,100%),1fr)); gap:16px; }}
.tp-kpi-grid > *, .tp-grid-2 > *, .tp-grid-3 > * {{ min-width:0; }}
@media (max-width:480px) {{ .tp-grid-2 {{ grid-template-columns:minmax(0,1fr); }} }}

/* ---------- KPI tiles ---------- */
.tp-kpi {{ padding:18px 20px; gap:6px; }}
.tp-kpi .label {{ font-size:.8rem; font-weight:500; color:var(--tp-muted); }}
.tp-kpi .value {{ font-size:1.75rem; line-height:2.1rem; font-weight:650; color:var(--tp-ink); letter-spacing:-.025em; font-variant-numeric:tabular-nums; }}
.tp-kpi .hint {{ font-size:.76rem; color:var(--tp-muted); }}
.tp-kpi .icon {{ width:36px; height:36px; border-radius:10px; display:flex; align-items:center; justify-content:center; font-size:19px; }}
.tp-tone-primary {{ background:#EFF4FF; color:#2563EB; }} .tp-tone-violet {{ background:#F4F3FF; color:#6938EF; }}
.tp-tone-green {{ background:#ECFDF3; color:#079455; }} .tp-tone-amber {{ background:#FFFAEB; color:#DC6803; }}
.tp-tone-sky {{ background:#F0F9FF; color:#0086C9; }} .tp-tone-rose {{ background:#FFF1F3; color:#E31B54; }}
.tp-tone-grey {{ background:#F2F4F7; color:#475467; }}

/* ---------- score ring ---------- */
.tp-score {{ --v:0; --c:var(--tp-brand); width:64px; height:64px; border-radius:50%; flex:none;
  background:conic-gradient(var(--c) calc(var(--v)*1%), #EAECF0 0); display:flex; align-items:center; justify-content:center; }}
.tp-score > span {{ width:50px; height:50px; border-radius:50%; background:#fff; display:flex; align-items:center; justify-content:center;
  font-weight:650; font-size:1rem; color:var(--tp-ink); font-variant-numeric:tabular-nums; }}
.tp-score.lg {{ width:96px; height:96px; }} .tp-score.lg > span {{ width:78px; height:78px; font-size:1.5rem; }}

/* ---------- buttons ---------- */
.q-btn {{ text-transform:none; font-weight:550; letter-spacing:0; border-radius:8px; }}
.q-btn:not(.q-btn--round):not(.q-btn--dense) {{ min-height:38px; padding:0 14px; }}
.q-btn--dense:not(.q-btn--round) {{ padding:2px 10px; min-height:32px; }}
.q-btn--rounded {{ border-radius:999px; }}
.q-btn .q-icon.on-left {{ margin-right:8px; font-size:1.15em; }}
.q-btn--unelevated.bg-primary, .q-btn--unelevated.bg-secondary {{ box-shadow:0 1px 2px rgba(16,24,40,.1), inset 0 1px 0 rgba(255,255,255,.14); }}
.q-btn--unelevated.bg-primary:hover, .q-btn--unelevated.bg-secondary:hover {{ background:var(--tp-brand-dark)!important; }}
.q-btn--outline.text-primary, .q-btn--outline.text-secondary, .q-btn--outline:not([class*='text-']) {{ color:var(--tp-text)!important; background:#fff; box-shadow:var(--tp-shadow-xs); }}
.q-btn--outline.text-primary:before, .q-btn--outline.text-secondary:before, .q-btn--outline:not([class*='text-']):before {{ border-color:var(--tp-border-strong); }}
.q-btn--outline.text-primary:hover, .q-btn--outline.text-secondary:hover {{ background:#F9FAFB; }}
.q-btn--outline.text-negative {{ background:#fff; }}
.q-btn--flat:not([class*='text-']) {{ color:var(--tp-text); }}
.q-btn .q-focus-helper {{ border-radius:inherit; }}
.q-btn.disabled {{ opacity:.45!important; }}

/* ---------- form fields ---------- */
.q-field--outlined .q-field__control {{ border-radius:8px; background:#fff; }}
.q-field--outlined .q-field__control:before {{ border-color:var(--tp-border-strong); transition:border-color .15s; }}
.q-field--outlined:hover .q-field__control:before {{ border-color:#98A2B3; }}
.q-field--outlined.q-field--focused .q-field__control {{ box-shadow:0 0 0 4px rgba(37,99,235,.12); }}
.q-field--outlined.q-field--focused .q-field__control:after {{ border-width:1px; border-color:var(--tp-brand); }}
.q-field--rounded .q-field__control {{ border-radius:999px; }}
.q-field__label {{ color:var(--tp-muted); font-weight:450; }}
.q-field__native, .q-field__input {{ color:var(--tp-ink); }}
.q-field--disabled .q-field__control {{ background:#F9FAFB; }}
.q-field__bottom {{ padding-top:4px; }}
.q-chip {{ border-radius:6px; background:#F2F4F7; color:var(--tp-text); font-weight:500; }}
.q-checkbox__bg {{ border-radius:5px; border-width:1.5px; }}
.q-toggle__track {{ opacity:.35; }}

/* ---------- badges ---------- */
.q-badge {{ font-size:.74rem; font-weight:550; line-height:1.1rem; padding:1px 8px; border-radius:999px; min-height:20px; letter-spacing:0; gap:2px; }}
.q-badge .q-icon {{ font-size:14px; }}
.q-badge--outline {{ border-width:1px; }}
{_badge_css()}
.q-badge--floating {{ background:#F04438!important; color:#fff!important; box-shadow:0 0 0 2px #fff!important; padding:0 5px; min-height:16px; font-size:.65rem; top:2px; right:0; }}

/* ---------- tables ---------- */
.q-table__container {{ border-radius:var(--tp-radius); }}
.q-table__card {{ box-shadow:none; }}
.q-table thead tr {{ background:#F9FAFB; }}
.q-table th {{ font-weight:600; color:#475467; font-size:.75rem; letter-spacing:.01em; text-transform:none; height:42px; }}
.q-table td {{ font-size:.875rem; color:var(--tp-text); height:54px; }}
.q-table tbody tr {{ transition:background .12s; }}
.q-table tbody tr:hover {{ background:#F9FAFB; }}
.q-table th, .q-table td {{ border-color:var(--tp-border); }}
.q-table__bottom {{ border-color:var(--tp-border); color:var(--tp-muted); }}

/* ---------- tabs ---------- */
.q-tabs {{ border-bottom:1px solid var(--tp-border); }}
.q-tabs .q-tab {{ text-transform:none; min-height:44px; padding:0 2px; margin-right:24px; color:var(--tp-muted); opacity:1; }}
.q-tabs .q-tab__content {{ min-width:0; }}
.q-tabs .q-tab__label {{ font-size:.875rem; font-weight:550; letter-spacing:0; }}
.q-tabs .q-tab--active {{ color:var(--tp-ink); }}
.q-tabs .q-tab__indicator {{ height:2px; background:var(--tp-brand); border-radius:2px 2px 0 0; }}
.q-tabs .q-focus-helper {{ display:none; }}
.q-tab-panels.bg-transparent .q-tab-panel {{ padding:20px 0 0; }}
.q-tab-panel {{ display:flex; flex-direction:column; gap:14px; }}

/* ---------- overlays ---------- */
.q-dialog__inner > .q-card {{ border:none; border-radius:16px; box-shadow:var(--tp-shadow-lg); padding:24px; gap:14px; }}
.q-dialog__backdrop {{ background:rgba(16,24,40,.45); backdrop-filter:blur(2px); }}
.q-menu {{ border-radius:12px; border:1px solid var(--tp-border); box-shadow:var(--tp-shadow-lg); }}
.q-menu .q-item {{ border-radius:8px; margin:2px 6px; min-height:36px; }}
.q-tooltip {{ background:#101828; font-size:.75rem; border-radius:6px; padding:6px 9px; max-width:360px; }}
.q-notification {{ border-radius:10px; box-shadow:var(--tp-shadow-lg); font-weight:500; }}

/* ---------- misc components ---------- */
.q-expansion-item {{ border-radius:10px; }}
.q-expansion-item .q-item {{ border-radius:10px; min-height:44px; font-weight:550; color:var(--tp-ink); }}
.q-expansion-item.tp-card {{ overflow:hidden; }}
.q-linear-progress {{ border-radius:999px; height:6px!important; color:var(--tp-brand); }}
.q-linear-progress__track {{ opacity:.12; }}
.q-uploader {{ width:100%; box-shadow:none!important; border:1.5px dashed var(--tp-border-strong)!important; border-radius:12px; background:#FCFCFD; max-height:none; }}
.q-uploader:hover {{ border-color:#84ADFF!important; background:#F8FAFF; }}
.q-uploader__header {{ background:transparent!important; color:var(--tp-text)!important; }}
.q-uploader__header-content {{ padding:14px 14px; }}
.q-uploader__title {{ font-size:.85rem; font-weight:500; }}
.q-uploader__subtitle {{ display:none; }}
.q-uploader__list {{ min-height:0; padding:0 10px 4px; }}
.q-uploader__list:empty {{ display:none; }}
.q-stepper--flat {{ border:none; background:transparent; }}
.q-stepper__title {{ font-weight:600; color:var(--tp-ink); }}
.q-stepper__tab {{ padding:14px 0; }}
.q-timeline__title {{ font-size:.9rem; font-weight:600; color:var(--tp-ink); margin-bottom:2px; text-transform:capitalize; }}
.q-timeline__subtitle {{ font-size:.75rem; letter-spacing:0; text-transform:none; opacity:1; color:var(--tp-muted); font-weight:500; }}
.q-timeline__entry {{ padding-bottom:4px; }}
.q-message-name {{ font-size:.75rem; color:var(--tp-muted); font-weight:550; margin-bottom:2px; }}
.q-message-text {{ border-radius:14px; padding:10px 14px; min-height:0; }}
.q-message-text:last-child:before {{ display:none; }}
.q-message-text--received {{ color:#F2F4F7; border-radius:4px 14px 14px 14px; }}
.q-message-text-content--received {{ color:var(--tp-ink); }}
.q-message-text--sent {{ color:var(--tp-brand); border-radius:14px 4px 14px 14px; }}
.q-message-text-content--sent {{ color:#fff; }}
.q-message-text-content--sent a {{ color:#fff; }}
.q-message-text-content p:last-child {{ margin-bottom:0; }}
.nicegui-code {{ border-radius:10px; border:1px solid var(--tp-border); }}
.q-splitter__separator {{ background:var(--tp-border); }}
.q-item__label--caption {{ color:var(--tp-muted); }}

/* ---------- app shell ---------- */
.tp-header {{ background:rgba(255,255,255,.88)!important; backdrop-filter:saturate(180%) blur(10px); color:var(--tp-ink)!important; border-bottom:1px solid var(--tp-border); height:60px; }}
.tp-drawer {{ background:#fff; border-right:1px solid var(--tp-border); }}
.tp-drawer {{ display:flex; flex-direction:column; padding:0; gap:0; flex-wrap:nowrap; align-items:stretch; }}
.tp-drawer > * {{ min-width:0; }}
.tp-org-switch {{ min-width:0; max-width:100%; }}
.tp-org-switch .q-field__control-container, .tp-org-switch .q-field__native {{ min-width:0; }}
.tp-org-switch .q-field__native > span {{ overflow:hidden; white-space:nowrap; text-overflow:ellipsis; display:block; }}
.tp-logo {{ width:32px; height:32px; border-radius:9px; background:linear-gradient(135deg,#3B82F6 0%,#4F46E5 60%,#7C3AED 100%);
  display:flex; align-items:center; justify-content:center; color:#fff; box-shadow:0 2px 6px rgba(79,70,229,.35), inset 0 1px 0 rgba(255,255,255,.25); flex:none; }}
.tp-wordmark {{ font-weight:700; font-size:1.02rem; letter-spacing:-.02em; color:var(--tp-ink); }}
.tp-wordmark .ai {{ color:var(--tp-brand); }}
.tp-nav-section {{ font-size:.68rem; font-weight:600; letter-spacing:.08em; text-transform:uppercase; color:#98A2B3; padding:14px 12px 5px; }}
a.tp-nav-item {{ display:flex; align-items:center; gap:11px; padding:0 12px; height:35px; margin:1px 0; border-radius:8px; color:#475467;
  text-decoration:none; font-weight:550; font-size:.875rem; transition:background .12s,color .12s; position:relative; }}
a.tp-nav-item .q-icon {{ font-size:19px; color:#98A2B3; transition:color .12s; }}
a.tp-nav-item:hover {{ background:#F5F7FA; color:var(--tp-ink); }}
a.tp-nav-item:hover .q-icon {{ color:#667085; }}
a.tp-nav-item.active {{ background:var(--tp-brand-soft); color:var(--tp-brand-dark); }}
a.tp-nav-item.active .q-icon {{ color:var(--tp-brand); }}
.tp-org-switch .q-field__control {{ background:#F9FAFB!important; min-height:44px!important; }}
.tp-org-switch .q-field__native {{ font-weight:600; color:var(--tp-ink); }}
.tp-status-card {{ margin:12px; padding:12px; border-radius:10px; background:#F9FAFB; border:1px solid var(--tp-border); }}
.tp-search .q-field__control {{ background:#F5F7FA!important; height:38px!important; min-height:38px!important; }}
.tp-search .q-field__control:before {{ border-color:transparent!important; }}
.tp-search.q-field--focused .q-field__control {{ background:#fff!important; }}
.tp-kbd {{ font-size:.7rem; font-weight:600; color:#667085; border:1px solid var(--tp-border-strong); border-bottom-width:2px; border-radius:5px; padding:0 5px; background:#fff; line-height:1.1rem; }}
.tp-avatar {{ width:32px; height:32px; border-radius:50%; background:linear-gradient(135deg,#DBEAFE,#E0E7FF); color:#3730A3; font-weight:650; font-size:.78rem;
  display:flex; align-items:center; justify-content:center; flex:none; letter-spacing:.02em; }}
.tp-avatar.sm {{ width:26px; height:26px; font-size:.68rem; }}
.tp-icon-btn {{ color:#475467!important; }}
.tp-crumb {{ font-size:.85rem; color:var(--tp-muted); font-weight:500; }}
.tp-crumb b {{ color:var(--tp-ink); font-weight:600; }}

/* ---------- public / auth ---------- */
.tp-public {{ background:#fff; }}
.tp-hero {{ background:radial-gradient(900px 420px at 85% -10%, rgba(124,58,237,.10), transparent 60%),
  radial-gradient(900px 500px at 10% -20%, rgba(37,99,235,.12), transparent 60%), linear-gradient(180deg,#F8FAFF 0%,#fff 100%); }}
.tp-hero-title {{ font-size:clamp(2.2rem,4.6vw,3.6rem); line-height:1.06; font-weight:750; letter-spacing:-.04em; color:var(--tp-ink); }}
.tp-gradient-text {{ background:linear-gradient(90deg,#2563EB,#7C3AED); -webkit-background-clip:text; background-clip:text; color:transparent; }}
.tp-lead {{ font-size:1.1rem; line-height:1.75rem; color:#475467; max-width:600px; }}
.tp-section-title {{ font-size:clamp(1.6rem,3vw,2.25rem); font-weight:700; letter-spacing:-.03em; color:var(--tp-ink); line-height:1.15; text-align:center; }}
.tp-section-sub {{ color:#475467; font-size:1.02rem; text-align:center; max-width:640px; }}
.tp-pill {{ display:inline-flex; align-items:center; gap:8px; padding:4px 12px 4px 4px; border-radius:999px; background:#fff; border:1px solid var(--tp-border);
  box-shadow:var(--tp-shadow-xs); font-size:.8rem; font-weight:550; color:var(--tp-text); }}
.tp-pill .tag {{ background:var(--tp-brand-soft); color:var(--tp-brand-dark); border-radius:999px; padding:1px 8px; font-weight:600; }}
.tp-feature-icon {{ width:44px; height:44px; border-radius:11px; display:flex; align-items:center; justify-content:center; font-size:22px; }}
.tp-plan.featured {{ border:1.5px solid var(--tp-brand); box-shadow:0 12px 32px -12px rgba(37,99,235,.35); position:relative; }}
.tp-preview {{ border-radius:16px; border:1px solid var(--tp-border); box-shadow:0 24px 60px -20px rgba(16,24,40,.25); background:#fff; overflow:hidden; }}
.tp-preview-bar {{ height:36px; background:#F9FAFB; border-bottom:1px solid var(--tp-border); display:flex; align-items:center; gap:6px; padding:0 14px; }}
.tp-preview-bar i {{ width:10px; height:10px; border-radius:50%; background:#EAECF0; display:inline-block; }}
.tp-step-num {{ width:36px; height:36px; border-radius:10px; background:var(--tp-ink); color:#fff; font-weight:650; display:flex; align-items:center; justify-content:center; }}
.tp-cta-band {{ background:linear-gradient(135deg,#1E3A8A 0%,#312E81 60%,#4C1D95 100%); border-radius:20px; color:#fff; }}
.tp-footer {{ border-top:1px solid var(--tp-border); color:var(--tp-muted); }}
.tp-public-nav {{ position:sticky; top:0; z-index:10; background:rgba(255,255,255,.85); backdrop-filter:saturate(180%) blur(10px); border-bottom:1px solid transparent; }}
.tp-public-nav a.navlink {{ color:#475467; text-decoration:none; font-weight:550; font-size:.9rem; }}
.tp-public-nav a.navlink:hover {{ color:var(--tp-ink); }}
.tp-auth-aside {{ background:radial-gradient(700px 400px at 0% 0%, rgba(59,130,246,.35), transparent 60%),
  radial-gradient(600px 400px at 100% 100%, rgba(124,58,237,.35), transparent 60%), #0B1220; color:#fff; }}
.tp-auth-aside .muted {{ color:#98A2B3; }}
.tp-check {{ width:22px; height:22px; border-radius:50%; background:rgba(59,130,246,.18); color:#93C5FD; display:flex; align-items:center; justify-content:center; font-size:14px; flex:none; }}
"""

FONT_LINKS = ('<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
              '<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;450;500;550;600;650;700;750&display=swap" rel="stylesheet">')

CHART_THEME = {
    "color": CHART_COLORS,
    "textStyle": {"fontFamily": "Inter, system-ui, sans-serif", "color": MUTED},
    "categoryAxis": {"axisLine": {"lineStyle": {"color": BORDER}}, "axisTick": {"show": False}, "axisLabel": {"color": MUTED},
                     "splitLine": {"show": False}},
    "valueAxis": {"axisLine": {"show": False}, "axisLabel": {"color": "#98A2B3"}, "splitLine": {"lineStyle": {"color": "#F2F4F7"}}},
    "legend": {"textStyle": {"color": TEXT}, "icon": "circle", "itemWidth": 8, "itemHeight": 8},
    "tooltip": {"backgroundColor": "#fff", "borderColor": BORDER, "textStyle": {"color": INK, "fontSize": 12},
                "extraCssText": "box-shadow:0 8px 24px -6px rgba(16,24,40,.18);border-radius:8px;"},
}


def _important_layer(css: str) -> str:
    """Copy every `!important` declaration into the `theme` cascade layer.

    NiceGUI loads Quasar's `!important` utilities (`.bg-*`, `.text-*`) in a cascade layer, and layered `!important`
    beats unlayered `!important`. For important declarations the *earliest* layer wins, so re-declaring them in
    `theme` (NiceGUI's first layer) lets the design system override Quasar's colour utilities.
    """
    rules = []
    for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        decls = [d.strip() for d in body.split(";") if "!important" in d]
        if decls and not sel.strip().startswith("@"):
            rules.append(f"{sel.strip()}{{{';'.join(decls)}}}")
    return "@layer theme{" + "\n".join(rules) + "}"


THEME_CSS = CSS + _important_layer(CSS)


def apply_theme() -> None:
    ui.colors(primary=BRAND, secondary=BRAND, accent="#7C3AED", dark=INK, positive=POSITIVE, negative=NEGATIVE, warning=WARNING, info=INFO)
    ui.add_head_html(FONT_LINKS + f"<style>{THEME_CSS}</style>")


# ---- presentational helpers ------------------------------------------------------------------------------------

def chart(options: dict, **kwargs) -> ui.echart:
    """ECharts with the shared theme (palette, typography, axis styling)."""
    return ui.echart(options, theme=CHART_THEME, **kwargs)


def initials(name: str) -> str:
    parts = [p for p in (name or "").split() if p[:1].isalpha()]
    return ("".join(p[0] for p in parts[:2]) or "?").upper()


def back_link(label: str, href: str) -> None:
    with ui.element("a").props(f'href="{href}"').classes("tp-back"):
        ui.icon("arrow_back", size="16px")
        ui.label(label).classes("ellipsis").style("max-width:min(560px, calc(100vw - 72px))")


@contextmanager
def page_header(title: str, subtitle: str | None = None, *, back: tuple[str, str] | None = None, eyebrow: str | None = None,
                meta: Callable[[], None] | None = None) -> Iterator[ui.row]:
    """Standard page header; the `with` body renders the right-aligned action buttons."""
    with ui.row().classes("tp-page-header w-full items-end justify-between"):
        with ui.column().classes("gap-1 col").style("min-width:280px"):
            if back:
                back_link(*back)
            if eyebrow:
                ui.label(eyebrow).classes("tp-eyebrow")
            ui.label(title).classes("tp-h1")
            if subtitle:
                ui.label(subtitle).classes("tp-subtitle")
            if meta:
                with ui.row().classes("items-center gap-2 q-mt-xs"):
                    meta()
        with ui.row().classes("items-center gap-2") as actions:
            yield actions


@contextmanager
def panel(title: str | None = None, subtitle: str | None = None, *, icon: str | None = None, classes: str = "",
          actions: Callable[[], None] | None = None) -> Iterator[ui.card]:
    """A titled content card. The `with` body renders the card content."""
    with ui.card().classes(f"tp-card tp-panel w-full {classes}").props("flat") as card:
        if title:
            with ui.row().classes("tp-panel-head w-full items-center justify-between no-wrap gap-3"):
                with ui.row().classes("items-center gap-3 no-wrap col"):
                    if icon:
                        ui.icon(icon).classes("tp-panel-icon")
                    with ui.column().classes("gap-0 col"):
                        ui.label(title).classes("tp-h2")
                        if subtitle:
                            ui.label(subtitle).classes("tp-caption")
                if actions:
                    with ui.row().classes("items-center gap-1 no-wrap"):
                        actions()
        yield card


def kpi_grid() -> ui.element:
    return ui.element("div").classes("tp-kpi-grid w-full")


def kpi(label: str, value: str, icon: str, hint: str | None = None, link: str | None = None, tone: str = "primary") -> None:
    with ui.card().classes("tp-card tp-kpi" + (" cursor-pointer" if link else "")).props("flat") as c:
        with ui.row().classes("items-center justify-between w-full no-wrap"):
            ui.label(label).classes("label")
            ui.icon(icon).classes(f"icon tp-tone-{tone}")
        ui.label(value).classes("value")
        if hint:
            with ui.row().classes("items-center gap-1 no-wrap"):
                ui.label(hint).classes("hint")
                if link:
                    ui.icon("arrow_forward", size="13px").classes("tp-muted")
    if link:
        c.on("click", lambda: ui.navigate.to(link))


def score_ring(score: float | None, *, large: bool = False) -> None:
    v = max(0, min(100, float(score))) if score is not None else 0
    color = "#079455" if v >= 75 else "#DC6803" if v >= 45 else "#D92D20"
    label = f"{v:.0f}" if score is not None else "—"
    ui.html(f'<div class="tp-score{" lg" if large else ""}" style="--v:{v:.0f};--c:{color if score is not None else "#D0D5DD"}" '
            f'role="img" aria-label="Score {label} of 100"><span>{label}</span></div>')


def dot(color: str) -> None:
    ui.element("span").classes("tp-dot").style(f"background:{color}")


def status_badge(status: str | None) -> None:
    label, color, icon = STATUS.get(status or "", ((status or "—").replace("_", " "), "grey-6", "circle"))
    with ui.badge(color=color).classes("tp-chip"):
        ui.icon(icon).classes("q-mr-xs")
        ui.label(label)


def overall_badge(status: str | None) -> None:
    label, color = OVERALL.get(status or "", ((status or "Not assessed").replace("_", " ").capitalize(), "grey-6"))
    ui.badge(label, color=color).classes("tp-chip")


def soft_badge(text: str, color: str = "grey-7") -> None:
    ui.badge(text, color=color).classes("tp-chip")


def empty_state(icon: str, title: str, body: str, action_label: str | None = None, on_action=None) -> None:
    with ui.column().classes("items-center w-full text-center gap-2").style("padding:48px 24px"):
        with ui.element("div").style("width:56px;height:56px;border-radius:14px;background:#F2F4F7;display:flex;align-items:center;"
                                     "justify-content:center;box-shadow:inset 0 0 0 1px #EAECF0;margin-bottom:6px"):
            ui.icon(icon, size="28px").style("color:#667085")
        ui.label(title).classes("tp-h2").style("font-size:1.02rem")
        ui.label(body).classes("tp-muted").style("max-width:460px")
        if action_label and on_action:
            ui.button(action_label, icon="add" if "import" in action_label.lower() or "add" in action_label.lower() else None,
                      on_click=on_action).props("unelevated color=primary").classes("q-mt-sm")

"""Reproducible demo seed with clearly-labelled synthetic organizations, users, documents and tenders."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.context import TenantContext
from app.core.config import BASE_DIR
from app.db.models import CompanyProfile, Organization, OrganizationMember, Role, Tender, User
from app.db.tenancy import system_scoped
from app.services.billing import entitlements
from app.services.documents import service as documents
from app.services.organizations import auth_service, org_service
from app.services.tenders import service as tenders

SAMPLE = BASE_DIR / "sample_data"
DEMO_PASSWORD = os.environ.get("TP_DEMO_PASSWORD", "Demo@Pass2026")

VOLTEDGE_USERS = [
    ("owner@voltedge.example", "Aarav Shah (demo)", Role.OWNER),
    ("bids@voltedge.example", "Meera Iyer (demo)", Role.BID_MANAGER),
    ("review@voltedge.example", "Rohan Gupta (demo)", Role.REVIEWER),
    ("docs@voltedge.example", "Kiran Rao (demo)", Role.CONTRIBUTOR),
    ("viewer@voltedge.example", "Sana Qureshi (demo)", Role.VIEWER),
]
KAVERI_USERS = [("owner@kaveri.example", "Divya Nair (demo)", Role.OWNER)]

VOLTEDGE_DOCS = [
    "certificate_of_incorporation.pdf", "gst_registration_certificate.pdf", "udyam_registration.pdf",
    "iso_9001_certificate_2023.pdf", "audited_financial_statements.pdf", "ca_turnover_certificate.pdf",
    "product_catalogue.pdf", "completion_certificate_msedcl.pdf", "completion_certificate_metro.pdf",
]


@dataclass
class SeedResult:
    contexts: dict[str, TenantContext] = field(default_factory=dict)
    orgs: dict[str, Organization] = field(default_factory=dict)
    tenders: dict[str, Tender] = field(default_factory=dict)


def _user(db: Session, email: str, name: str) -> User:
    u = db.scalar(select(User).where(User.email == email))
    if u:
        return u
    u = auth_service.register_user(db, email=email, password=DEMO_PASSWORD, full_name=name)
    from app.db.base import utcnow

    u.email_verified_at = utcnow()
    return u


def _ctx(db: Session, user: User, org: Organization) -> TenantContext:
    m = db.scalar(select(OrganizationMember).where(OrganizationMember.user_id == user.id, OrganizationMember.organization_id == org.id))
    assert m is not None, "seed membership missing"
    return TenantContext(user_id=user.id, organization_id=org.id, role=m.role, email=user.email, channel="seed")


def _org(db: Session, owner: User, name: str, users: list, profile: dict) -> tuple[Organization, dict[str, TenantContext]]:
    org = db.scalar(select(Organization).where(Organization.name == name))
    if org is None:
        org = org_service.create_organization(db, owner, name, is_demo=True)
        db.flush()
    ctxs = {}
    for email, full, role in users:
        u = _user(db, email, full)
        if not db.scalar(select(OrganizationMember).where(OrganizationMember.user_id == u.id, OrganizationMember.organization_id == org.id)):
            db.add(OrganizationMember(organization_id=org.id, user_id=u.id, role=role))
            db.flush()
        ctxs[email] = _ctx(db, u, org)
    prof = db.scalar(select(CompanyProfile).where(CompanyProfile.organization_id == org.id))
    for k, v in profile.items():
        if getattr(prof, k) in (None, [], ""):
            setattr(prof, k, v)
    return org, ctxs


def _upload(db: Session, ctx: TenantContext, path: Path, **kw):
    return documents.upload(db, ctx, path.read_bytes(), path.name, is_synthetic=True, **kw)


@system_scoped
def seed(db: Session, *, with_tenders: bool = True) -> SeedResult:
    entitlements.ensure_plans(db)
    res = SeedResult()
    owner = _user(db, VOLTEDGE_USERS[0][0], VOLTEDGE_USERS[0][1])
    vorg, vctx = _org(db, owner, "VoltEdge Industries Pvt. Ltd. (Demo)", VOLTEDGE_USERS, {
        "legal_name": "VoltEdge Industries Private Limited (synthetic)", "industry": "Electrical equipment manufacturing",
        "business_category": "Manufacturer", "state": "Maharashtra", "operational_states": ["Maharashtra", "Gujarat", "Karnataka"],
        "capabilities": ["LT distribution panels", "PCC / MCC panels", "APFC panels", "Installation & commissioning"],
        "product_categories": ["LT panels", "switchgear assemblies", "power factor correction"],
        "tender_interests": ["LT panels", "substation", "switchgear", "distribution board", "electrical"],
        "onboarding_step": 5,
    })
    kowner = _user(db, KAVERI_USERS[0][0], KAVERI_USERS[0][1])
    korg, kctx = _org(db, kowner, "Kaveri Infraprojects Pvt. Ltd. (Demo)", KAVERI_USERS, {
        "legal_name": "Kaveri Infraprojects Private Limited (synthetic)", "industry": "Civil & EPC contracting", "state": "Karnataka",
        "operational_states": ["Karnataka", "Tamil Nadu"], "capabilities": ["water treatment plants", "civil works"],
        "tender_interests": ["water treatment", "civil", "pipeline"], "onboarding_step": 5,
    })
    res.orgs = {"voltedge": vorg, "kaveri": korg}
    res.contexts = {**vctx, **kctx}
    owner_ctx = vctx["owner@voltedge.example"]
    from app.db.models import Document

    have = set(db.scalars(select(Document.title).where(Document.organization_id == vorg.id)))
    for name in VOLTEDGE_DOCS:
        if name.rsplit(".", 1)[0].replace("_", " ") not in have:
            _upload(db, owner_ctx, SAMPLE / "voltedge" / name)
    kctx_owner = kctx["owner@kaveri.example"]
    have_k = set(db.scalars(select(Document.title).where(Document.organization_id == korg.id)))
    for name in ("iso_9001_certificate.pdf", "audited_financials.pdf"):
        if name.rsplit(".", 1)[0].replace("_", " ") not in have_k:
            _upload(db, kctx_owner, SAMPLE / "kaveri" / name)
    if with_tenders:
        bm = vctx["bids@voltedge.example"]
        res.tenders["lt_panels"] = _tender(db, bm, "MSGIC/PROC/2026/LT-118", "LT distribution panels for 12 substations (imported)",
                                           ["msgic_lt_panels_nit.pdf", "msgic_lt_panels_boq.xlsx"])
        res.tenders["street_lighting"] = _tender(db, bm, "PMSIL/ELEC/2026/77", "Street lighting feeder pillars (imported)", ["pmsil_street_lighting_rfp.pdf"])
        res.tenders["kaveri_wtp"] = _tender(db, kctx_owner, "KUWWB/CIV/2026/301", "5 MLD water treatment plant (imported)", ["kuwwb_wtp_nit.pdf"])
    return res


def _tender(db: Session, ctx: TenantContext, ref: str, title: str, files: list[str]) -> Tender:
    t = db.scalar(select(Tender).where(Tender.organization_id == ctx.organization_id, Tender.reference_number == ref))
    if t:
        return t
    t = tenders.create_tender(db, ctx, {"title": title, "reference_number": ref, "source_platform": "Manual upload (synthetic)"}, is_synthetic=True)
    t.extraction = {}  # let extraction fill fields; title/ref above are placeholders
    for f in files:
        tenders.attach_document(db, ctx, t, (SAMPLE / "tenders" / f).read_bytes(), f, "main", is_synthetic=True)
    return t

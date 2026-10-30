"""Tenant-isolated hybrid retrieval: Postgres full-text + pgvector cosine, fused with RRF, reranked.

organization_id is a mandatory argument taken from the server-resolved TenantContext; there is
no code path that searches across organizations.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.ai.retrieval.embeddings import get_embedder
from app.auth.context import TenantContext
from app.auth.permissions import Perm

RRF_K = 60


@dataclass
class SearchFilters:
    scope: str | None = None  # company | tender | bid
    categories: list[str] | None = None
    document_ids: list[uuid.UUID] | None = None
    tender_id: uuid.UUID | None = None
    current_versions_only: bool = True


@dataclass
class Hit:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    document_title: str
    category: str
    page_no: int
    section: str | None
    text: str
    bbox: list[float] | None
    lexical_rank: int | None = None
    vector_rank: int | None = None
    vector_similarity: float = 0.0
    score: float = 0.0
    signals: dict = field(default_factory=dict)


def _where(f: SearchFilters, params: dict) -> str:
    clauses = ["c.organization_id = :org", "d.deleted_at IS NULL"]
    if f.current_versions_only:
        clauses.append("d.current_version_id = c.version_id")
    if f.scope:
        clauses.append("d.scope = :scope")
        params["scope"] = f.scope
    if f.categories:
        clauses.append("d.category = ANY(:cats)")
        params["cats"] = list(f.categories)
    if f.document_ids:
        clauses.append("d.id = ANY(:docids)")
        params["docids"] = list(f.document_ids)
    if f.tender_id:
        clauses.append("d.tender_id = :tender")
        params["tender"] = f.tender_id
    return " AND ".join(clauses)


_SELECT = """
SELECT c.id, c.document_id, c.version_id, d.title, d.category, c.page_no, c.section, c.text, c.bbox
FROM document_chunks c JOIN documents d ON d.id = c.document_id
"""


def hybrid_search(
    db: Session, ctx: TenantContext, query: str, *, filters: SearchFilters | None = None, limit: int = 8
) -> list[Hit]:
    ctx.require(Perm.DOCUMENTS_READ)
    f = filters or SearchFilters()
    query = query.strip()[:1000]
    if not query:
        return []
    params: dict = {"org": ctx.organization_id, "q": query, "n": limit * 4}
    where = _where(f, params)
    lexical = db.execute(
        text(
            _SELECT
            + f" WHERE {where} AND c.tsv @@ websearch_to_tsquery('english', :q)"
            " ORDER BY ts_rank_cd(c.tsv, websearch_to_tsquery('english', :q)) DESC LIMIT :n"
        ),
        params,
    ).all()
    # Fallback OR-query when the AND-semantics websearch query is too strict.
    if len(lexical) < limit:
        terms = [t for t in re.findall(r"[A-Za-z0-9]{3,}", query)][:12]
        if terms:
            params["orq"] = " | ".join(terms)
            lexical += db.execute(
                text(
                    _SELECT
                    + f" WHERE {where} AND c.tsv @@ to_tsquery('english', :orq)"
                    " ORDER BY ts_rank_cd(c.tsv, to_tsquery('english', :orq)) DESC LIMIT :n"
                ),
                params,
            ).all()
    embedder = get_embedder()
    qvec = embedder.embed([query])[0]
    params["qv"] = str(qvec)
    params["model"] = embedder.model_name
    vector = db.execute(
        text(
            "SELECT c.id, c.document_id, c.version_id, d.title, d.category, c.page_no, c.section, c.text, c.bbox,"
            " 1 - (c.embedding <=> CAST(:qv AS vector)) AS sim"
            " FROM document_chunks c JOIN documents d ON d.id = c.document_id"
            f" WHERE {where} AND c.embedding IS NOT NULL AND c.embedding_model = :model"
            " ORDER BY c.embedding <=> CAST(:qv AS vector) LIMIT :n"
        ),
        params,
    ).all()

    hits: dict[uuid.UUID, Hit] = {}
    for rank, row in enumerate(lexical):
        h = hits.setdefault(row[0], Hit(*row[:9]))
        if h.lexical_rank is None:
            h.lexical_rank = rank
    for rank, row in enumerate(vector):
        h = hits.setdefault(row[0], Hit(*row[:9]))
        h.vector_rank, h.vector_similarity = rank, float(row[9])
    qterms = set(re.findall(r"[a-z0-9]{3,}", query.lower()))
    for h in hits.values():
        rrf = 0.0
        if h.lexical_rank is not None:
            rrf += 1.0 / (RRF_K + h.lexical_rank)
        if h.vector_rank is not None:
            rrf += 1.0 / (RRF_K + h.vector_rank)
        overlap = len(qterms & set(re.findall(r"[a-z0-9]{3,}", h.text.lower()))) / max(len(qterms), 1)
        h.score = rrf * 30 + overlap  # lightweight lexical-overlap rerank
        h.signals = {"rrf": round(rrf, 5), "term_overlap": round(overlap, 2), "vector_similarity": round(h.vector_similarity, 3)}
    return sorted(hits.values(), key=lambda h: h.score, reverse=True)[:limit]


def retrieval_confidence(hits: list[Hit]) -> float:
    """Heuristic 0–1 signal used for abstention: driven by term overlap of the top hit."""
    if not hits:
        return 0.0
    top = hits[0]
    both = 0.15 if (top.lexical_rank is not None and top.vector_rank is not None) else 0.0
    return round(min(1.0, top.signals.get("term_overlap", 0) * 0.85 + both), 2)

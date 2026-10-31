"""RAG operations: configurable reranking, privacy-preserving query log + retention, incremental reindex."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

pytestmark = pytest.mark.usefixtures("seeded")


def test_reranker_modes_and_hashed_query_log(seeded, db, monkeypatch):
    from app.ai.retrieval import search
    from app.core.config import get_settings
    from app.db.models import RetrievalQueryLog

    ctx = seeded.contexts["bids@voltedge.example"]
    q = "ISO 9001 certificate validity"
    hits = search.hybrid_search(db, ctx, q, limit=5)
    assert hits and all("term_overlap" in h.signals for h in hits)
    db.flush()
    row = db.scalar(select(RetrievalQueryLog).where(RetrievalQueryLog.query_sha256 == search.query_digest(q)).order_by(RetrievalQueryLog.created_at.desc()))
    assert row.query_text is None and row.reranker == "overlap" and row.result_count == len(hits)
    assert row.organization_id == ctx.organization_id and 0 <= row.confidence <= 1

    monkeypatch.setattr(get_settings(), "rerank_mode", "none")
    plain = search.hybrid_search(db, ctx, q, limit=5, log=False)
    assert all(h.score == pytest.approx(h.signals["rrf"] * 30) for h in plain)
    db.rollback()


def test_query_text_opt_in_and_retention(seeded, db, monkeypatch):
    from app.ai.retrieval import search
    from app.core.config import get_settings
    from app.db.base import utcnow
    from app.db.models import RetrievalQueryLog
    from app.services.organizations.retention import purge_retrieval_logs

    ctx = seeded.contexts["bids@voltedge.example"]
    monkeypatch.setattr(get_settings(), "retrieval_log_query_text", True)
    search.hybrid_search(db, ctx, "average annual turnover", limit=3)
    db.flush()
    row = db.scalar(select(RetrievalQueryLog).where(RetrievalQueryLog.query_text == "average annual turnover"))
    assert row is not None
    db.execute(update(RetrievalQueryLog).where(RetrievalQueryLog.id == row.id).values(created_at=utcnow() - timedelta(days=400)))
    assert purge_retrieval_logs(db) >= 1
    assert db.get(RetrievalQueryLog, row.id) is None or db.scalar(select(func.count()).where(RetrievalQueryLog.id == row.id)) == 0
    db.rollback()


def test_incremental_reindex_only_touches_stale_chunks(seeded, db):
    from app.ai.retrieval.reindex import reindex, stale_count
    from app.db.models import DocumentChunk

    org = seeded.contexts["owner@kaveri.example"].bind().organization_id
    ids = db.scalars(select(DocumentChunk.id).where(DocumentChunk.organization_id == org).limit(3)).all()
    assert ids and stale_count(db, org) == 0
    db.execute(update(DocumentChunk).where(DocumentChunk.id.in_(ids)).values(embedding_model="old-model-v0"))
    db.commit()
    assert stale_count(db, org) == len(ids)
    out = reindex(db, org)
    assert out["reembedded"] == len(ids) and stale_count(db, org) == 0

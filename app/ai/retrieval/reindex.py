"""Incremental re-embedding: only chunks whose vector is missing or was produced by a different
embedding model are recomputed (e.g. after switching TP_EMBEDDING_PROVIDER). Search ignores stale
vectors (it filters on embedding_model), so results stay consistent while a reindex runs."""

from __future__ import annotations

import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.ai.retrieval.embeddings import _cached_hash_vec, get_embedder
from app.core.logging import get_logger
from app.db.models import DocumentChunk

log = get_logger(__name__)


def _stale(model: str, org_id: uuid.UUID | None):
    cond = or_(DocumentChunk.embedding.is_(None), DocumentChunk.embedding_model.is_(None), DocumentChunk.embedding_model != model)
    return (cond, DocumentChunk.organization_id == org_id) if org_id else (cond,)


def stale_count(db: Session, org_id: uuid.UUID | None = None) -> int:
    return db.scalar(select(func.count()).select_from(DocumentChunk).where(*_stale(get_embedder().model_name, org_id))) or 0


def reindex(db: Session, org_id: uuid.UUID | None = None, *, batch: int = 256, full: bool = False) -> dict:
    """Re-embed stale chunks (or every chunk with full=True), committing per batch so it can resume."""
    embedder = get_embedder()
    if full:
        _cached_hash_vec.cache_clear()
    done, last_id = 0, None
    while True:
        q = select(DocumentChunk).order_by(DocumentChunk.id).limit(batch)
        if org_id:
            q = q.where(DocumentChunk.organization_id == org_id)
        if full:
            q = q.where(DocumentChunk.id > last_id) if last_id else q
        else:
            q = q.where(*_stale(embedder.model_name, org_id))
        rows = db.scalars(q).all()
        if not rows:
            break
        vecs = embedder.embed([(c.section or "") + "\n" + c.text for c in rows])
        for c, v in zip(rows, vecs, strict=True):
            c.embedding, c.embedding_model = v, embedder.model_name
        db.commit()
        done += len(rows)
        last_id = rows[-1].id
        log.info("reindex.batch", chunks=done, model=embedder.model_name)
    return {"reembedded": done, "model": embedder.model_name}

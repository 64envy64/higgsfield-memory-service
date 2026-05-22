"""Search service.

Different shape from recall: structured `SearchResult[]` rather than a formatted
context blob. Reuses the same retrievers and the same RRF fusion as `/recall`,
but skips the gate (search is explicitly invoked by an agent tool — the agent
made the decision to retrieve).

Scope rules (per contract):
  user_id != null            → user-scope
  user_id == null && session → session-scope
  both null                  → empty (no global search)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import asyncpg

from memory_service.config import Settings
from memory_service.embedding.client import EmbeddingClient
from memory_service.schemas.search import SearchIn, SearchOut, SearchResult
from memory_service.services import retrievers
from memory_service.services.fusion import group_by_source, rrf_fuse
from memory_service.services.retrievers import Candidate

logger = logging.getLogger(__name__)


async def search(
    *,
    payload: SearchIn,
    pool: asyncpg.Pool,
    settings: Settings,
    embedder: EmbeddingClient,
) -> SearchOut:
    # Scope resolution per the contract.
    if payload.user_id:
        scope_type, scope_id = "user", payload.user_id
    elif payload.session_id:
        scope_type, scope_id = "session", payload.session_id
    else:
        return SearchOut(results=[])

    results = await asyncio.gather(
        retrievers.vector_turns(
            pool, embedder=embedder, query=payload.query,
            scope_type=scope_type, scope_id=scope_id, limit=payload.limit,
        ),
        retrievers.fts_turns(
            pool, query=payload.query,
            scope_type=scope_type, scope_id=scope_id, limit=payload.limit,
        ),
        retrievers.vector_memories(
            pool, embedder=embedder, query=payload.query,
            scope_type=scope_type, scope_id=scope_id, limit=payload.limit,
        ),
        retrievers.fts_memories(
            pool, query=payload.query,
            scope_type=scope_type, scope_id=scope_id, limit=payload.limit,
        ),
        return_exceptions=True,
    )

    candidates: list[Candidate] = []
    for r in results:
        if isinstance(r, BaseException):
            logger.warning("retriever failed in search: %s", r)
            continue
        candidates.extend(r)

    fused = rrf_fuse(group_by_source(candidates), k=settings.rrf_k)
    top = fused[: payload.limit]

    results_out: list[SearchResult] = []
    for c, fused_score in top:
        results_out.append(
            SearchResult(
                content=c.content,
                score=round(fused_score, 6),
                session_id=c.session_id or "",
                # Memories use their `updated_at` (set by the retriever);
                # turns use their original `timestamp`. Falling back to "now"
                # would be misleading — only do so if we genuinely have nothing.
                timestamp=c.timestamp or datetime.now(timezone.utc),
                metadata={"kind": c.kind, "source": c.source, **c.metadata},
            )
        )
    return SearchOut(results=results_out)

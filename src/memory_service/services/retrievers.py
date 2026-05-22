"""Candidate retrievers.

Each retriever returns an ordered list of `Candidate`. Higher rank = better.
Score scales (cosine vs ts_rank_cd) are kept separate; the downstream fusion
layer (v0.4 RRF) handles normalization.

Per Invariant 1, every retriever is parameterized by (scope_type, scope_id).

Concurrency note: retrievers accept the connection pool and acquire their own
connection. asyncpg connections do not support concurrent statements on the
same connection, so each parallel retriever gets its own.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from memory_service.embedding.client import EmbeddingClient
from memory_service.util.text import to_or_tsquery

logger = logging.getLogger(__name__)


@dataclass
class Candidate:
    """A scored retrieval hit. Used uniformly across turn / memory / graph retrievers.

    `source_turn` is the turn id that backs this candidate's content:
      - for kind='turn'   it's the turn itself (== id).
      - for kind='memory' it's the turn the memory was extracted from. Used by
        the assembler to emit a Citation pointing at usable source provenance
        instead of a memory UUID that the consumer can't resolve.
    """
    source: str                              # 'turn_vector','turn_fts','memory_vector','memory_fts','graph'
    kind: str                                # 'turn' | 'memory'
    id: str                                  # turn_id or memory_id (str)
    score: float                             # source-specific raw score
    content: str                             # text usable as a snippet
    session_id: str | None = None
    timestamp: Any = None
    source_turn: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def _embedding_param(vec: list[float]) -> str:
    """pgvector accepts a text literal like '[0.1,0.2,...]'."""
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def _with_prior(base_meta: dict[str, Any], row: asyncpg.Record) -> dict[str, Any]:
    """Augment a memory-candidate metadata dict with a `prior` chain entry, if any.

    Used by every memory retriever so Tier 2 of the assembler can render arcs
    ("Currently X (previously: Y, until DATE)") exactly the same way Tier 1
    does — the v0.11 symmetry fix. `until` date is the *new* memory's source-
    turn timestamp (when the user actually stated the contradiction), falling
    back to the prior's updated_at if the source_turn was deleted.
    """
    if not row.get("prior_id"):
        return base_meta
    until_ts = row.get("m_turn_ts") or row.get("prior_updated_at")
    return {
        **base_meta,
        "prior": {
            "id": str(row["prior_id"]),
            "value": row.get("prior_value"),
            "object": row.get("prior_object"),
            "updated_at": until_ts,
        },
    }


def _coerce_jsonb(value: Any) -> dict[str, Any]:
    """asyncpg connections that lack the jsonb codec hand back a str; tolerate both."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


# ----------------------------------------------------------------------------- turn retrievers
async def vector_turns(
    pool: asyncpg.Pool,
    *,
    embedder: EmbeddingClient,
    query: str,
    scope_type: str,
    scope_id: str,
    limit: int = 10,
) -> list[Candidate]:
    """Cosine-similarity top-k against turns within scope. No-op if embedder is disabled."""
    if not embedder.is_enabled:
        return []
    vec = await embedder.embed(query)
    if vec is None:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, session_id, timestamp, full_text, metadata,
                   1 - (embedding <=> $1::vector) AS score
            FROM turns
            WHERE scope_type = $2 AND scope_id = $3
              AND embedding IS NOT NULL
            ORDER BY embedding <=> $1::vector
            LIMIT $4
            """,
            _embedding_param(vec), scope_type, scope_id, limit,
        )
    return [
        Candidate(
            source="turn_vector",
            kind="turn",
            id=str(r["id"]),
            score=float(r["score"]),
            content=r["full_text"],
            session_id=r["session_id"],
            timestamp=r["timestamp"],
            source_turn=str(r["id"]),
            metadata=_coerce_jsonb(r["metadata"]),
        )
        for r in rows
    ]


async def fts_turns(
    pool: asyncpg.Pool,
    *,
    query: str,
    scope_type: str,
    scope_id: str,
    limit: int = 10,
) -> list[Candidate]:
    """Postgres FTS (ts_rank_cd) top-k against turns within scope, OR-semantics.

    `plainto_tsquery` ANDs tokens, which kills natural-language questions whose
    keyword overlap with the stored turn is partial. We instead build an OR
    tsquery from stop-filtered content tokens and let ts_rank_cd order by overlap.
    """
    tsq = to_or_tsquery(query)
    if not tsq:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, session_id, timestamp, full_text, metadata,
                   ts_rank_cd(tsv, q) AS score
            FROM turns, to_tsquery('english', $1) q
            WHERE scope_type = $2 AND scope_id = $3
              AND tsv @@ q
            ORDER BY score DESC
            LIMIT $4
            """,
            tsq, scope_type, scope_id, limit,
        )
    return [
        Candidate(
            source="turn_fts",
            kind="turn",
            id=str(r["id"]),
            score=float(r["score"]),
            content=r["full_text"],
            session_id=r["session_id"],
            timestamp=r["timestamp"],
            source_turn=str(r["id"]),
            metadata=_coerce_jsonb(r["metadata"]),
        )
        for r in rows
    ]


# ----------------------------------------------------------------------------- memory retrievers
async def vector_memories(
    pool: asyncpg.Pool,
    *,
    embedder: EmbeddingClient,
    query: str,
    scope_type: str,
    scope_id: str,
    limit: int = 10,
) -> list[Candidate]:
    if not embedder.is_enabled:
        return []
    vec = await embedder.embed(query)
    if vec is None:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT m.id, m.value, m.source_session, m.source_turn, m.updated_at,
                   m.type, m.key, m.confidence,
                   1 - (m.embedding <=> $1::vector) AS score,
                   t.timestamp  AS m_turn_ts,
                   p.id         AS prior_id,
                   p.value      AS prior_value,
                   p.object     AS prior_object,
                   p.updated_at AS prior_updated_at
            FROM memories m
            LEFT JOIN memories p ON p.id = m.supersedes
            LEFT JOIN turns t    ON t.id = m.source_turn
            WHERE m.scope_type = $2 AND m.scope_id = $3
              AND m.active = true
              AND m.embedding IS NOT NULL
            ORDER BY m.embedding <=> $1::vector
            LIMIT $4
            """,
            _embedding_param(vec), scope_type, scope_id, limit,
        )
    return [
        Candidate(
            source="memory_vector",
            kind="memory",
            id=str(r["id"]),
            score=float(r["score"]),
            content=r["value"],
            session_id=r["source_session"],
            timestamp=r["updated_at"],
            source_turn=str(r["source_turn"]) if r["source_turn"] else None,
            metadata=_with_prior(
                {"type": r["type"], "key": r["key"], "confidence": float(r["confidence"])},
                r,
            ),
        )
        for r in rows
    ]


async def fts_memories(
    pool: asyncpg.Pool,
    *,
    query: str,
    scope_type: str,
    scope_id: str,
    limit: int = 10,
) -> list[Candidate]:
    tsq = to_or_tsquery(query)
    if not tsq:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            WITH q AS (SELECT to_tsquery('english', $1) AS tsq)
            SELECT m.id, m.value, m.source_session, m.source_turn, m.updated_at,
                   m.type, m.key, m.confidence,
                   ts_rank_cd(m.tsv, q.tsq) AS score,
                   t.timestamp  AS m_turn_ts,
                   p.id         AS prior_id,
                   p.value      AS prior_value,
                   p.object     AS prior_object,
                   p.updated_at AS prior_updated_at
            FROM memories m
            CROSS JOIN q
            LEFT JOIN memories p ON p.id = m.supersedes
            LEFT JOIN turns t    ON t.id = m.source_turn
            WHERE m.scope_type = $2 AND m.scope_id = $3
              AND m.active = true
              AND m.tsv @@ q.tsq
            ORDER BY score DESC
            LIMIT $4
            """,
            tsq, scope_type, scope_id, limit,
        )
    return [
        Candidate(
            source="memory_fts",
            kind="memory",
            id=str(r["id"]),
            score=float(r["score"]),
            content=r["value"],
            session_id=r["source_session"],
            timestamp=r["updated_at"],
            source_turn=str(r["source_turn"]) if r["source_turn"] else None,
            metadata=_with_prior(
                {"type": r["type"], "key": r["key"], "confidence": float(r["confidence"])},
                r,
            ),
        )
        for r in rows
    ]

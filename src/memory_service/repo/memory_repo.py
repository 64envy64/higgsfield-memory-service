from __future__ import annotations

import uuid
from typing import Any

import asyncpg

from memory_service.schemas.memories import MemoryOut


def _row_to_memory(row: asyncpg.Record) -> MemoryOut:
    return MemoryOut(
        id=str(row["id"]),
        type=row["type"],
        subject=row["subject"],
        predicate=row["predicate"],
        object=row["object"],
        key=row["key"],
        value=row["value"],
        raw_quote=row["raw_quote"],
        confidence=float(row["confidence"]),
        source_session=row["source_session"],
        source_turn=str(row["source_turn"]) if row["source_turn"] else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        supersedes=str(row["supersedes"]) if row["supersedes"] else None,
        active=row["active"],
    )


async def list_stable_facts(
    conn: asyncpg.Connection,
    *,
    scope_type: str,
    scope_id: str,
    min_confidence: float = 0.5,
    limit: int = 24,
) -> list[dict[str, Any]]:
    """Return active, high-confidence user facts and preferences for Tier 1 of recall.

    Also includes both 'opinion' and 'preference' types so the assembler can
    optionally render their supersession chain (arc surfacing). The query
    LEFT JOINs the immediate prior memory via `supersedes` so the assembler
    doesn't need a second round-trip per row to render "currently X (was Y)".

    Ordered: facts before preferences before opinions (descending stability),
    then by confidence desc, then by recency.
    """
    rows = await conn.fetch(
        """
        SELECT m.id, m.type, m.subject, m.predicate, m.object, m.value,
               m.confidence, m.source_turn, m.updated_at,
               t.timestamp   AS m_turn_ts,
               p.id          AS prior_id,
               p.value       AS prior_value,
               p.object      AS prior_object,
               p.updated_at  AS prior_updated_at,
               p.type        AS prior_type
        FROM memories m
        LEFT JOIN memories p ON p.id = m.supersedes
        LEFT JOIN turns t ON t.id = m.source_turn
        WHERE m.scope_type = $1 AND m.scope_id = $2
          AND m.active = true
          AND m.type IN ('fact','preference','opinion')
          AND m.confidence >= $3
        ORDER BY CASE m.type
                     WHEN 'fact' THEN 0
                     WHEN 'preference' THEN 1
                     ELSE 2
                 END,
                 m.confidence DESC,
                 m.updated_at DESC
        LIMIT $4
        """,
        scope_type, scope_id, min_confidence, limit,
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        item = {
            "id": str(r["id"]),
            "type": r["type"],
            "subject": r["subject"],
            "predicate": r["predicate"],
            "object": r["object"],
            "value": r["value"],
            "confidence": float(r["confidence"]),
            "source_turn": str(r["source_turn"]) if r["source_turn"] else None,
            "updated_at": r["updated_at"],
            "prior": None,
        }
        if r["prior_id"]:
            # "until" date for the prior = when the user *stated* the new fact,
            # which is m.source_turn.timestamp (the user-provided turn timestamp).
            # Fall back to prior.updated_at if the source_turn was deleted
            # (ON DELETE SET NULL) since the chain is then ambiguous.
            until_ts = r["m_turn_ts"] or r["prior_updated_at"]
            item["prior"] = {
                "id": str(r["prior_id"]),
                "value": r["prior_value"],
                "object": r["prior_object"],
                "updated_at": until_ts,
                "type": r["prior_type"],
            }
        out.append(item)
    return out


async def entities_for_names(
    conn: asyncpg.Connection,
    *,
    scope_type: str,
    scope_id: str,
    names: list[str],
) -> list[dict[str, Any]]:
    """Lookup entities by name (case-insensitive). Returns empty list if names is empty."""
    if not names:
        return []
    rows = await conn.fetch(
        """
        SELECT id, name, type
        FROM entities
        WHERE scope_type = $1 AND scope_id = $2
          AND lower(name) = ANY($3::text[])
        """,
        scope_type, scope_id, [n.lower() for n in names if n],
    )
    return [{"id": r["id"], "name": r["name"], "type": r["type"]} for r in rows]


async def memories_mentioning_entities(
    conn: asyncpg.Connection,
    *,
    entity_ids: list,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Memories whose mentions point to any of the given entities. Active only.

    Also returns the 1-step supersession prior (if any) so the assembler can
    render the arc when this memory ends up in Tier 2.
    """
    if not entity_ids:
        return []
    rows = await conn.fetch(
        """
        SELECT m.id, m.subject, m.predicate, m.object, m.value,
               m.type, m.confidence, m.source_turn, m.source_session, m.updated_at,
               t.timestamp  AS m_turn_ts,
               p.id         AS prior_id,
               p.value      AS prior_value,
               p.object     AS prior_object,
               p.updated_at AS prior_updated_at
        FROM memories m
        JOIN memory_entity_mentions mem ON mem.memory_id = m.id
        LEFT JOIN memories p ON p.id = m.supersedes
        LEFT JOIN turns t    ON t.id = m.source_turn
        WHERE mem.entity_id = ANY($1::uuid[])
          AND m.active = true
        ORDER BY m.confidence DESC
        LIMIT $2
        """,
        entity_ids, limit,
    )
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in rows:
        mid = str(r["id"])
        if mid in seen:
            continue
        seen.add(mid)
        item = {
            "id": mid,
            "subject": r["subject"],
            "predicate": r["predicate"],
            "object": r["object"],
            "value": r["value"],
            "type": r["type"],
            "confidence": float(r["confidence"]),
            "source_turn": str(r["source_turn"]) if r["source_turn"] else None,
            "source_session": r["source_session"],
            "updated_at": r["updated_at"],
            "prior": None,
        }
        if r["prior_id"]:
            until_ts = r["m_turn_ts"] or r["prior_updated_at"]
            item["prior"] = {
                "id": str(r["prior_id"]),
                "value": r["prior_value"],
                "object": r["prior_object"],
                "updated_at": until_ts,
            }
        out.append(item)
    return out


async def list_by_user(conn: asyncpg.Connection, user_id: str) -> list[MemoryOut]:
    """Return all memories (active + superseded) for a user, newest first."""
    rows = await conn.fetch(
        """
        SELECT id, type, subject, predicate, object, key, value, raw_quote,
               confidence, source_session, source_turn, created_at, updated_at,
               supersedes, active
        FROM memories
        WHERE scope_type = 'user' AND scope_id = $1
        ORDER BY created_at DESC, id
        """,
        user_id,
    )
    return [_row_to_memory(r) for r in rows]


async def delete_by_session(conn: asyncpg.Connection, session_id: str) -> int:
    res = await conn.execute("DELETE FROM memories WHERE source_session = $1", session_id)
    try:
        return int(res.split()[-1])
    except (ValueError, IndexError):
        return 0


async def delete_by_user(conn: asyncpg.Connection, user_id: str) -> int:
    res = await conn.execute(
        """
        DELETE FROM memories
        WHERE (scope_type = 'user' AND scope_id = $1)
        """,
        user_id,
    )
    try:
        return int(res.split()[-1])
    except (ValueError, IndexError):
        return 0


async def affected_scope_keys(
    conn: asyncpg.Connection, source_session: str
) -> list[tuple[str, str, str]]:
    """Capture (scope_type, scope_id, key) tuples touched by a session, prior to delete."""
    rows = await conn.fetch(
        "SELECT DISTINCT scope_type, scope_id, key FROM memories WHERE source_session = $1",
        source_session,
    )
    return [(r["scope_type"], r["scope_id"], r["key"]) for r in rows]


async def insert_memory(
    conn: asyncpg.Connection,
    *,
    memory_id: uuid.UUID,
    scope_type: str,
    scope_id: str,
    type: str,
    subject: str,
    predicate: str,
    object_: str,
    value: str,
    raw_quote: str | None,
    confidence: float,
    source_session: str | None,
    source_turn: uuid.UUID | None,
    supersedes: uuid.UUID | None,
    active: bool,
    embedding: list[float] | None,
) -> None:
    """Insert a single memory row. Caller is responsible for transaction boundary."""
    embedding_param: str | None = None
    if embedding is not None:
        embedding_param = "[" + ",".join(f"{x:.6f}" for x in embedding) + "]"
    await conn.execute(
        """
        INSERT INTO memories
            (id, scope_type, scope_id, type, subject, predicate, object,
             value, raw_quote, confidence, source_session, source_turn,
             supersedes, active, embedding)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15::vector)
        """,
        memory_id, scope_type, scope_id, type, subject, predicate, object_,
        value, raw_quote, confidence, source_session, source_turn,
        supersedes, active, embedding_param,
    )


async def upsert_entity(
    conn: asyncpg.Connection,
    *,
    scope_type: str,
    scope_id: str,
    name: str,
    type_: str | None,
) -> uuid.UUID:
    """Insert-or-fetch an entity row. Returns the entity id (existing or new)."""
    new_id = uuid.uuid4()
    row = await conn.fetchrow(
        """
        INSERT INTO entities (id, scope_type, scope_id, name, type)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (scope_type, scope_id, lower(name), coalesce(type, ''))
            DO UPDATE SET name = EXCLUDED.name
        RETURNING id
        """,
        new_id, scope_type, scope_id, name, type_,
    )
    return row["id"]


async def link_mention(
    conn: asyncpg.Connection,
    *,
    memory_id: uuid.UUID,
    entity_id: uuid.UUID,
) -> None:
    await conn.execute(
        """
        INSERT INTO memory_entity_mentions (memory_id, entity_id)
        VALUES ($1, $2) ON CONFLICT DO NOTHING
        """,
        memory_id, entity_id,
    )


async def insert_edge(
    conn: asyncpg.Connection,
    *,
    src_memory: uuid.UUID,
    dst_memory: uuid.UUID,
    relation: str,
    weight: float = 1.0,
) -> None:
    """Insert a directed memory→memory edge. Idempotent on (src, dst, relation)."""
    if src_memory == dst_memory:
        return
    await conn.execute(
        """
        INSERT INTO memory_edges (src_memory, dst_memory, relation, weight)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT DO NOTHING
        """,
        src_memory, dst_memory, relation, weight,
    )


async def active_memories_with_subject(
    conn: asyncpg.Connection,
    *,
    scope_type: str,
    scope_id: str,
    subject: str,
    exclude_id: uuid.UUID | None = None,
) -> list[uuid.UUID]:
    """Find active memories in scope sharing the given subject (for same_subject edges)."""
    rows = await conn.fetch(
        """
        SELECT id FROM memories
        WHERE scope_type = $1 AND scope_id = $2 AND subject = $3
          AND active = true
          AND ($4::uuid IS NULL OR id <> $4)
        """,
        scope_type, scope_id, subject, exclude_id,
    )
    return [r["id"] for r in rows]


async def memories_via_edges(
    conn: asyncpg.Connection,
    *,
    src_memory_ids: list[uuid.UUID],
    limit: int = 12,
) -> list[dict[str, Any]]:
    """1-hop traversal of memory_edges from a set of seed memory IDs.

    Returns active neighbors with the strongest edge weight per (src, dst).
    Used by the recall pipeline to enrich the candidate pool with memories
    that are graph-related to the top retriever hits — e.g. co-extracted from
    the same turn as a strong-match memory.
    """
    if not src_memory_ids:
        return []
    rows = await conn.fetch(
        """
        SELECT m.id, m.value, m.subject, m.predicate, m.object, m.type,
               m.confidence, m.source_turn, m.source_session, m.updated_at,
               max(e.weight)                  AS edge_weight,
               array_agg(DISTINCT e.relation) AS relations,
               max(t.timestamp)               AS m_turn_ts,
               max(p.id::text)                AS prior_id,
               max(p.value)                   AS prior_value,
               max(p.object)                  AS prior_object,
               max(p.updated_at)              AS prior_updated_at
        FROM memory_edges e
        JOIN memories m       ON m.id = e.dst_memory
        LEFT JOIN memories p  ON p.id = m.supersedes
        LEFT JOIN turns t     ON t.id = m.source_turn
        WHERE e.src_memory = ANY($1::uuid[])
          AND m.active = true
          AND NOT (m.id = ANY($1::uuid[]))
        GROUP BY m.id
        ORDER BY edge_weight DESC, m.confidence DESC
        LIMIT $2
        """,
        src_memory_ids, limit,
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        item = {
            "id": str(r["id"]),
            "value": r["value"],
            "subject": r["subject"],
            "predicate": r["predicate"],
            "object": r["object"],
            "type": r["type"],
            "confidence": float(r["confidence"]),
            "source_turn": str(r["source_turn"]) if r["source_turn"] else None,
            "source_session": r["source_session"],
            "updated_at": r["updated_at"],
            "edge_weight": float(r["edge_weight"]),
            "relations": list(r["relations"] or []),
            "prior": None,
        }
        if r["prior_id"]:
            until_ts = r["m_turn_ts"] or r["prior_updated_at"]
            item["prior"] = {
                "id": str(r["prior_id"]),
                "value": r["prior_value"],
                "object": r["prior_object"],
                "updated_at": until_ts,
            }
        out.append(item)
    return out


async def recompute_active(
    conn: asyncpg.Connection, scope_type: str, scope_id: str, key: str
) -> None:
    """After deletions, ensure exactly one memory per (scope,key) is active = the latest."""
    await conn.execute(
        """
        UPDATE memories SET active = false
        WHERE scope_type = $1 AND scope_id = $2 AND key = $3
        """,
        scope_type, scope_id, key,
    )
    await conn.execute(
        """
        UPDATE memories SET active = true
        WHERE id = (
            SELECT id FROM memories
            WHERE scope_type = $1 AND scope_id = $2 AND key = $3
            ORDER BY created_at DESC
            LIMIT 1
        )
        """,
        scope_type, scope_id, key,
    )

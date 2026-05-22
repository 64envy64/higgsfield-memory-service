from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import asyncpg

from memory_service.schemas.turns import TurnIn


def scope_for(user_id: str | None, session_id: str) -> tuple[str, str]:
    """Resolve (scope_type, scope_id) per Invariant 1.

    If user_id is provided, memories follow the user across sessions.
    Otherwise the scope falls back to the session (anonymous mode).
    """
    if user_id:
        return ("user", user_id)
    return ("session", session_id)


def flatten_messages(messages: list[Any]) -> str:
    """Render messages into a single text block suitable for FTS and embedding."""
    parts: list[str] = []
    for m in messages:
        role = m.role if hasattr(m, "role") else m["role"]
        content = (m.content if hasattr(m, "content") else m["content"]) or ""
        name = m.name if hasattr(m, "name") else m.get("name")
        prefix = f"{role}({name})" if name else role
        parts.append(f"{prefix}: {content.strip()}")
    return "\n".join(parts)


async def insert_turn(
    conn: asyncpg.Connection,
    *,
    turn_id: uuid.UUID,
    payload: TurnIn,
    embedding: list[float] | None,
) -> None:
    scope_type, scope_id = scope_for(payload.user_id, payload.session_id)
    full_text = flatten_messages(payload.messages)
    messages_value = [m.model_dump() for m in payload.messages]
    metadata_value = payload.metadata

    embedding_param: str | None = None
    if embedding is not None:
        embedding_param = "[" + ",".join(f"{x:.6f}" for x in embedding) + "]"

    await conn.execute(
        """
        INSERT INTO turns (id, session_id, user_id, scope_type, scope_id,
                           messages, full_text, timestamp, metadata, embedding)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9::jsonb, $10::vector)
        """,
        turn_id,
        payload.session_id,
        payload.user_id,
        scope_type,
        scope_id,
        messages_value,
        full_text,
        payload.timestamp,
        metadata_value,
        embedding_param,
    )


async def delete_by_session(conn: asyncpg.Connection, session_id: str) -> int:
    res = await conn.execute("DELETE FROM turns WHERE session_id = $1", session_id)
    # asyncpg returns "DELETE <n>"
    try:
        return int(res.split()[-1])
    except (ValueError, IndexError):
        return 0


async def delete_by_user(conn: asyncpg.Connection, user_id: str) -> int:
    res = await conn.execute("DELETE FROM turns WHERE user_id = $1", user_id)
    try:
        return int(res.split()[-1])
    except (ValueError, IndexError):
        return 0


async def turn_exists(conn: asyncpg.Connection, turn_id: uuid.UUID) -> bool:
    row = await conn.fetchrow("SELECT 1 FROM turns WHERE id = $1", turn_id)
    return row is not None


async def turn_age_seconds(conn: asyncpg.Connection, turn_id: uuid.UUID) -> float | None:
    row = await conn.fetchrow(
        "SELECT EXTRACT(EPOCH FROM (now() - ingested_at))::float AS age FROM turns WHERE id=$1",
        turn_id,
    )
    return float(row["age"]) if row else None


async def latest_turn_timestamp(conn: asyncpg.Connection) -> datetime | None:
    row = await conn.fetchrow("SELECT max(timestamp) AS t FROM turns")
    return row["t"] if row else None

from __future__ import annotations

import logging

from fastapi import APIRouter, Response, status

from memory_service.api.deps import AuthDep, PoolDep
from memory_service.repo import memory_repo, turn_repo

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health")
async def health(pool: PoolDep) -> dict[str, str]:
    """Liveness + readiness. Public (no auth) so orchestrators can probe it."""
    try:
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
    except Exception as e:                            # pragma: no cover - defensive
        logger.warning("health: db ping failed: %s", e)
        # Returning 200 with a status field would be ambiguous for orchestrators;
        # we explicitly return 503 when the backing store is unreachable.
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="db unavailable") from e
    return {"status": "ok"}


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: str,
    pool: PoolDep,
    _: AuthDep,
) -> Response:
    """Delete a session: capture affected (scope,key) → delete memories → delete turns → recompute active.

    Also drops session-scope entities — for anonymous turns (user_id=null),
    entities were created with scope=('session', session_id) and would otherwise
    linger after the memories that mention them are gone.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            affected = await memory_repo.affected_scope_keys(conn, session_id)
            await memory_repo.delete_by_session(conn, session_id)
            await turn_repo.delete_by_session(conn, session_id)
            # Drop session-scoped entities for this session. memory_entity_mentions
            # are already CASCADE-cleared by the memory deletes above.
            await conn.execute(
                "DELETE FROM entities WHERE scope_type = 'session' AND scope_id = $1",
                session_id,
            )
            for scope_type, scope_id, key in affected:
                await memory_repo.recompute_active(conn, scope_type, scope_id, key)
    logger.info("session %s deleted (affected_keys=%d)", session_id, len(affected))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: str,
    pool: PoolDep,
    _: AuthDep,
) -> Response:
    """Delete everything for a user: memories, entities, turns."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM entities WHERE scope_type = 'user' AND scope_id = $1",
                user_id,
            )
            await memory_repo.delete_by_user(conn, user_id)
            await turn_repo.delete_by_user(conn, user_id)
    logger.info("user %s deleted", user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

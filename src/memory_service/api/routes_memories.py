from __future__ import annotations

import logging

from fastapi import APIRouter

from memory_service.api.deps import AuthDep, PoolDep
from memory_service.repo import memory_repo
from memory_service.schemas.memories import MemoriesResponse

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/users/{user_id}/memories", response_model=MemoriesResponse)
async def list_user_memories(
    user_id: str,
    pool: PoolDep,
    _: AuthDep,
) -> MemoriesResponse:
    """Inspect all memories (active + superseded) for a user."""
    async with pool.acquire() as conn:
        memories = await memory_repo.list_by_user(conn, user_id)
    return MemoriesResponse(memories=memories)

from __future__ import annotations

import logging

from fastapi import APIRouter

from memory_service.api.deps import AuthDep, PoolDep, SettingsDep
from memory_service.embedding.client import get_embedding_client
from memory_service.schemas.search import SearchIn, SearchOut
from memory_service.services import search as search_service

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/search", response_model=SearchOut)
async def search(
    payload: SearchIn,
    pool: PoolDep,
    settings: SettingsDep,
    _: AuthDep,
) -> SearchOut:
    """Structured search invoked by agent tool calls.

    Scope rules enforced in `services.search`:
      user_id != null            → user-scope
      user_id == null && session → session-scope
      both null                  → empty (no global search)
    """
    embedder = get_embedding_client(settings)
    return await search_service.search(
        payload=payload, pool=pool, settings=settings, embedder=embedder
    )

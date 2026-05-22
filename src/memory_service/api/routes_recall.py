from __future__ import annotations

import logging

from fastapi import APIRouter

from memory_service.api.deps import AuthDep, PoolDep, SettingsDep
from memory_service.embedding.client import get_embedding_client
from memory_service.schemas.recall import RecallIn, RecallOut
from memory_service.services import recall as recall_service

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/recall", response_model=RecallOut)
async def recall(
    payload: RecallIn,
    pool: PoolDep,
    settings: SettingsDep,
    _: AuthDep,
) -> RecallOut:
    """Return formatted context for the next agent turn.

    Per Invariant 3, the default on uncertainty is empty. The recall service
    handles query analysis, hybrid retrieval, relevance gating, and tiered
    context assembly under the caller's token budget.
    """
    embedder = get_embedding_client(settings)
    return await recall_service.recall(
        payload=payload, pool=pool, settings=settings, embedder=embedder
    )

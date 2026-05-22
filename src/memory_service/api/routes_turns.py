from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import APIRouter, status

from memory_service.api.deps import AuthDep, PoolDep, SettingsDep
from memory_service.embedding.client import get_embedding_client
from memory_service.extraction.service import get_extraction_service
from memory_service.llm.client import get_llm_client
from memory_service.repo import memory_repo, turn_repo
from memory_service.repo.turn_repo import flatten_messages, scope_for
from memory_service.schemas.turns import TurnIn, TurnOut
from memory_service.services import reconciler

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/turns", response_model=TurnOut, status_code=status.HTTP_201_CREATED)
async def ingest_turn(
    payload: TurnIn,
    pool: PoolDep,
    settings: SettingsDep,
    _: AuthDep,
) -> TurnOut:
    """Ingest a completed turn. Synchronous per I2 — returns only after the raw
    turn + extracted memories + entity edges are all committed in a single
    transaction. After 201 every write is visible to /recall, /search and
    /users/{id}/memories.
    """
    turn_id = uuid.uuid4()
    full_text = flatten_messages(payload.messages)
    scope_type, scope_id = scope_for(payload.user_id, payload.session_id)

    # --- Phase 1: out-of-transaction network calls (embed + LLM extract).
    # Doing these inside the transaction would hold a connection for tens of
    # seconds; doing them first means the transaction itself is fast (just inserts).
    embedder = get_embedding_client(settings)
    llm = get_llm_client(settings)
    extractor = get_extraction_service(settings, llm)

    async def _embed_turn() -> list[float] | None:
        return await embedder.embed(full_text) if embedder.is_enabled else None

    turn_embedding, candidates = await asyncio.gather(
        _embed_turn(),
        extractor.extract(full_text),
    )

    # Batch-embed memory values (best effort; per-element None on failure).
    if candidates and embedder.is_enabled:
        memory_vecs = await embedder.embed_batch([c.value for c in candidates])
    else:
        memory_vecs = [None] * len(candidates)

    # --- Phase 2: single transaction commits everything (I2).
    async with pool.acquire() as conn:
        async with conn.transaction():
            await turn_repo.insert_turn(
                conn,
                turn_id=turn_id,
                payload=payload,
                embedding=turn_embedding,
            )

            inserted_memories = 0
            superseded_memories = 0
            inserted_ids: list[uuid.UUID] = []
            inserted_subjects: list[str] = []
            for cand, vec in zip(candidates, memory_vecs, strict=True):
                # v0.5: serialize per-(scope,key) reconciles via advisory lock,
                # then decide insert/supersede/skip. All inside the same txn so
                # /recall never sees a partial state.
                await reconciler.acquire_lock(
                    conn, scope_type=scope_type, scope_id=scope_id, key=cand.key()
                )
                decision = await reconciler.reconcile(
                    conn, scope_type=scope_type, scope_id=scope_id, candidate=cand
                )
                await reconciler.apply_decision(conn, decision)

                if not decision.insert:
                    continue

                if decision.supersedes is not None:
                    superseded_memories += 1
                inserted_memories += 1

                mem_id = uuid.uuid4()
                await memory_repo.insert_memory(
                    conn,
                    memory_id=mem_id,
                    scope_type=scope_type,
                    scope_id=scope_id,
                    type=cand.type,
                    subject=cand.subject,
                    predicate=cand.predicate,
                    object_=cand.object,
                    value=cand.value,
                    raw_quote=cand.raw_quote or None,
                    confidence=cand.confidence,
                    source_session=payload.session_id,
                    source_turn=turn_id,
                    supersedes=decision.supersedes,
                    active=decision.active,
                    embedding=vec,
                )
                inserted_ids.append(mem_id)
                inserted_subjects.append(cand.subject)

                # Entity edges: upsert each named entity, link the memory to it.
                for ent in cand.entities:
                    if not ent.name:
                        continue
                    eid = await memory_repo.upsert_entity(
                        conn,
                        scope_type=scope_type,
                        scope_id=scope_id,
                        name=ent.name,
                        type_=ent.type,
                    )
                    await memory_repo.link_mention(conn, memory_id=mem_id, entity_id=eid)

            # --- memory_edges (v0.6): link co-extracted memories from this turn
            # and same-subject memories with prior actives. Directed edges in both
            # directions so 1-hop traversal works from either endpoint.
            for i, src_id in enumerate(inserted_ids):
                for j, dst_id in enumerate(inserted_ids):
                    if i == j:
                        continue
                    await memory_repo.insert_edge(
                        conn,
                        src_memory=src_id, dst_memory=dst_id,
                        relation="co_extracted", weight=0.7,
                    )
                # same_subject across all active memories sharing the subject
                peers = await memory_repo.active_memories_with_subject(
                    conn,
                    scope_type=scope_type, scope_id=scope_id,
                    subject=inserted_subjects[i], exclude_id=src_id,
                )
                for peer_id in peers:
                    await memory_repo.insert_edge(
                        conn,
                        src_memory=src_id, dst_memory=peer_id,
                        relation="same_subject", weight=0.5,
                    )
                    await memory_repo.insert_edge(
                        conn,
                        src_memory=peer_id, dst_memory=src_id,
                        relation="same_subject", weight=0.5,
                    )

    logger.info(
        "turn ingested",
        extra={
            "turn_id": str(turn_id),
            "session_id": payload.session_id,
            "msgs": len(payload.messages),
            "embedded": turn_embedding is not None,
            "candidates": len(candidates),
            "memories_inserted": inserted_memories,
            "memories_superseded": superseded_memories,
        },
    )
    return TurnOut(id=str(turn_id))

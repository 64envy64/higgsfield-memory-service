"""Recall service (v0.8).

Pipeline:
  1. QueryAnalyzer → intent, profile_relevant, entities, expansions.
  2. Retrievers (vector + FTS over turns and memories) in parallel.
  3. Entity-anchored memory lookup (if query mentions known entities).
  4. Stable-facts lookup (always fetched; gate decides whether to inject).
  5. Relevance gate (Invariant 3 — empty over wrong).
  6. Tiered context assembler with hard token budget.
"""
from __future__ import annotations

import asyncio
import logging

import asyncpg

from memory_service.config import Settings
from memory_service.embedding.client import EmbeddingClient
from memory_service.llm.client import get_llm_client
from memory_service.repo import memory_repo
from memory_service.repo.turn_repo import scope_for
from memory_service.schemas.recall import RecallIn, RecallOut
from memory_service.services import query_analyzer, retrievers
from memory_service.services.assembler import TierBudget, assemble, decide_gate
from memory_service.services.fusion import group_by_source, rrf_fuse
from memory_service.services.retrievers import Candidate

logger = logging.getLogger(__name__)


# Different sources, different score scales — we need different floors per kind.
# Vector hits use cosine similarity (settings.min_relevance_cosine, typically
# 0.30+). FTS hits use ts_rank_cd which tops out around 0.3 for strong matches;
# we set a low but non-zero floor to keep single-word coincidental overlap from
# opening the gate. Graph-anchored memory candidates have a synthetic high
# score (see services/recall.py — entity hop assigns 0.9), which clears both.
_TURN_FTS_MIN = 0.05      # ts_rank_cd hits on turns must be above coincidence
_MEMORY_FTS_MIN = 0.01    # any meaningful FTS hit on a structured memory counts


def _has_memory_signal(memory_cands: list[Candidate], *, cosine_min: float) -> bool:
    for c in memory_cands:
        if c.source == "memory_vector" and c.score >= cosine_min:
            return True
        if c.source == "memory_fts" and c.score >= _MEMORY_FTS_MIN:
            return True
        if c.source == "graph":         # entity-anchored memories are always strong
            return True
    return False


def _has_turn_signal(turn_cands: list[Candidate], *, cosine_min: float) -> bool:
    for c in turn_cands:
        if c.source == "turn_vector" and c.score >= cosine_min:
            return True
        if c.source == "turn_fts" and c.score >= _TURN_FTS_MIN:
            return True
    return False


async def recall(
    *,
    payload: RecallIn,
    pool: asyncpg.Pool,
    settings: Settings,
    embedder: EmbeddingClient,
) -> RecallOut:
    scope_type, scope_id = scope_for(payload.user_id, payload.session_id)
    llm = get_llm_client(settings)

    # --- 1. Analyze the query (LLM if available, else heuristic).
    analysis = await query_analyzer.analyze(payload.query, llm=llm)

    # --- 2. Fan out: retrievers in parallel, stable-facts and entities also.
    queries_to_run = [payload.query, *analysis.expanded_queries]

    async def _run_retrievers_for(q: str) -> list[Candidate]:
        results = await asyncio.gather(
            retrievers.vector_turns(
                pool, embedder=embedder, query=q,
                scope_type=scope_type, scope_id=scope_id,
                limit=settings.default_recall_k,
            ),
            retrievers.fts_turns(
                pool, query=q,
                scope_type=scope_type, scope_id=scope_id,
                limit=settings.default_recall_k,
            ),
            retrievers.vector_memories(
                pool, embedder=embedder, query=q,
                scope_type=scope_type, scope_id=scope_id,
                limit=settings.default_recall_k,
            ),
            retrievers.fts_memories(
                pool, query=q,
                scope_type=scope_type, scope_id=scope_id,
                limit=settings.default_recall_k,
            ),
            return_exceptions=True,
        )
        flat: list[Candidate] = []
        for r in results:
            if isinstance(r, BaseException):
                logger.warning("retriever failed: %s", r)
                continue
            flat.extend(r)
        return flat

    # Run retrievers for the original query first; expansions add coverage but
    # are bounded to 2 so we don't N-times the DB load.
    retriever_results = await asyncio.gather(
        *[_run_retrievers_for(q) for q in queries_to_run[:3]]
    )
    all_cands: list[Candidate] = []
    for chunk in retriever_results:
        all_cands.extend(chunk)

    # Stable facts: always fetched; gate decides whether to inject. Cheap query
    # and lets us answer "what do you remember about me?" without retriever hits.
    # Run sequentially — three small queries, single conn (no risk of asyncpg
    # "another operation in progress").
    async with pool.acquire() as conn:
        stable_facts = await memory_repo.list_stable_facts(
            conn, scope_type=scope_type, scope_id=scope_id,
            min_confidence=0.5, limit=16,
        )
        matched_entities = await memory_repo.entities_for_names(
            conn, scope_type=scope_type, scope_id=scope_id,
            names=analysis.entities,
        )
        entity_memories: list[dict] = []
        if matched_entities:
            entity_memories = await memory_repo.memories_mentioning_entities(
                conn, entity_ids=[e["id"] for e in matched_entities], limit=12,
            )

    # Inject entity-anchored memories as memory_candidates.
    if entity_memories:
        for em in entity_memories:
            md = {
                "type": em["type"],
                "predicate": em["predicate"],
                "object": em["object"],
                "confidence": em["confidence"],
            }
            if em.get("prior"):
                md["prior"] = em["prior"]
            all_cands.append(Candidate(
                source="graph",
                kind="memory",
                id=em["id"],
                score=0.9,                          # high — they hit a named entity
                content=em["value"],
                session_id=em.get("source_session"),
                timestamp=em.get("updated_at"),
                source_turn=em.get("source_turn"),
                metadata=md,
            ))

    # v0.6: 1-hop traversal of memory_edges from the top-N current memory
    # candidates. Picks up `co_extracted` (same turn) and `same_subject`
    # neighbors that didn't independently hit FTS/vector — e.g. user said
    # "I work at Notion and moved to Berlin" → "Notion" matches employer
    # memory; the location memory is a co_extracted edge away.
    import uuid as _uuid
    seed_memory_ids = [
        _uuid.UUID(c.id) for c in all_cands if c.kind == "memory"
    ][:8]
    if seed_memory_ids:
        async with pool.acquire() as conn:
            edge_memories = await memory_repo.memories_via_edges(
                conn, src_memory_ids=seed_memory_ids, limit=12,
            )
        for em in edge_memories:
            # Score = edge weight scaled into the cosine-ish range.
            # `co_extracted` weight 0.7 → score 0.7; `same_subject` 0.5 → 0.5.
            md = {
                "type": em["type"],
                "predicate": em["predicate"],
                "object": em["object"],
                "confidence": em["confidence"],
                "edge_relations": em.get("relations", []),
            }
            if em.get("prior"):
                md["prior"] = em["prior"]
            all_cands.append(Candidate(
                source="edge_hop",
                kind="memory",
                id=em["id"],
                score=float(em["edge_weight"]),
                content=em["value"],
                session_id=em.get("source_session"),
                timestamp=em.get("updated_at"),
                source_turn=em.get("source_turn"),
                metadata=md,
            ))

    # --- 3. Reciprocal Rank Fusion across heterogeneous sources.
    # Score scales differ wildly between cosine/ts_rank/graph priors; RRF is
    # rank-based so we don't need to normalize. Source weights bias the result
    # toward structured-memory hits (see services.fusion.SOURCE_WEIGHTS).
    fused = rrf_fuse(group_by_source(all_cands), k=settings.rrf_k)

    # The assembler still wants memory-first ordering for budget triage:
    # within RRF-sorted output, push memories ahead of turns at equal-ish
    # scores. Stable sort preserves RRF ranking within each kind.
    fused.sort(key=lambda x: (x[0].kind != "memory", -x[1]))

    memory_cands = [c for c, _ in fused if c.kind == "memory"]
    turn_cands = [c for c, _ in fused if c.kind == "turn"]

    # --- 4. Compute gate.
    gate = decide_gate(
        profile_relevant=analysis.profile_relevant,
        is_open_ended_about_user=(
            analysis.profile_relevant
            and analysis.intent in ("exploratory", "recent_context")
        ),
        has_memory_signal=_has_memory_signal(
            memory_cands, cosine_min=settings.min_relevance_cosine,
        ),
        has_turn_signal=_has_turn_signal(
            turn_cands, cosine_min=settings.min_relevance_cosine,
        ),
        has_entity_match=bool(matched_entities),
    )

    logger.info(
        "recall analyzed",
        extra={
            "intent": analysis.intent,
            "profile_relevant": analysis.profile_relevant,
            "entities_in_query": analysis.entities,
            "entity_matches": len(matched_entities),
            "memory_cands": len(memory_cands),
            "turn_cands": len(turn_cands),
            "tier1_open": gate.tier1_open,
            "tier2_open": gate.tier2_open,
        },
    )

    # --- 5. Assemble.
    # Arc surfacing on Tier 1 (opinions/preferences) is enabled only when the
    # query plausibly asks about evolution: exploratory ("what does the user
    # think about X now?") or factoid_about_user (which the analyzer assigns
    # to "tell me what they prefer / what they like"). Simple factoid queries
    # about other topics stay compact.
    render_arc = analysis.intent in ("exploratory", "factoid_about_user")

    budget = TierBudget(max_tokens=payload.max_tokens)
    context, citations = assemble(
        stable_facts=stable_facts,
        memory_candidates=memory_cands,
        turn_candidates=turn_cands,
        gate=gate,
        budget=budget,
        render_arc=render_arc,
    )
    return RecallOut(context=context, citations=citations)

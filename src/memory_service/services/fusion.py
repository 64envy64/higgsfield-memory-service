"""Reciprocal Rank Fusion (RRF) over heterogeneous retrievers.

Why RRF and not, say, weighted-sum of normalized scores:
  * Score scales differ wildly across our sources — pgvector cosine ∈ [0,1],
    ts_rank_cd ∈ [0, ~0.5], graph-anchored memories are assigned a fixed
    high prior. Z-score / min-max normalization gets noisy on small N.
  * RRF is rank-based, so the relative scales don't matter — it only cares
    where each item lands in its source's ordering.
  * Standard formula (Cormack/Clarke/Buettcher 2009):
        score(d) = Σ over sources s : weight(s) / (k + rank_s(d))
    with `k=60` as the de-facto default and per-source weights to encode
    "memories are stronger evidence than turns" without fully discarding
    turn evidence.

Per source weights below are calibrated empirically against the self-fixture
(memory_* preferred over turn_*; graph hop preferred slightly over plain
retrieval). They can be retuned without changing call sites.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from memory_service.services.retrievers import Candidate


# Higher = stronger source. Sum across sources doesn't need to be 1.
# graph        : entity-anchored memories — strongest evidence.
# memory_vector: semantic match on a structured memory.
# memory_fts   : keyword match on a structured memory.
# edge_hop     : 1-hop neighbor of a top memory candidate (co_extracted /
#                same_subject in memory_edges) — secondary evidence.
# turn_vector  : semantic match on raw turn text.
# turn_fts     : keyword match on raw turn text — noisiest.
SOURCE_WEIGHTS: dict[str, float] = {
    "memory_vector": 1.4,
    "memory_fts":    1.1,
    "graph":         1.5,
    "edge_hop":      0.9,
    "turn_vector":   1.0,
    "turn_fts":      0.7,
}


def rrf_fuse(
    candidates_per_source: dict[str, list[Candidate]],
    *,
    k: int = 60,
) -> list[tuple[Candidate, float]]:
    """Fuse multiple ranked lists via weighted RRF.

    Input: {source_name: [Candidate in rank order]}.
    Output: [(best_candidate_for_id, fused_score)] sorted by score desc.

    When the same (kind, id) appears across multiple sources, we keep the
    Candidate from the highest-weighted source so the downstream consumer
    sees the most informative metadata (vector score over FTS score, etc.).
    """
    fused_score: dict[tuple[str, str], float] = defaultdict(float)
    chosen_cand: dict[tuple[str, str], tuple[Candidate, float]] = {}

    for source, cands in candidates_per_source.items():
        weight = SOURCE_WEIGHTS.get(source, 1.0)
        for rank, c in enumerate(cands):
            key = (c.kind, c.id)
            fused_score[key] += weight / (k + rank + 1)   # rank is 0-based here

            # Keep the candidate from the strongest source seen so far.
            prev = chosen_cand.get(key)
            if prev is None or weight > prev[1]:
                chosen_cand[key] = (c, weight)

    fused = [(chosen_cand[key][0], score) for key, score in fused_score.items()]
    fused.sort(key=lambda x: x[1], reverse=True)
    return fused


def group_by_source(candidates: Iterable[Candidate]) -> dict[str, list[Candidate]]:
    """Bucket a flat candidate list by their `source` tag, preserving rank order.

    Retrievers already return their lists in score-descending order, so the
    enumeration position is the rank. We don't re-sort here.
    """
    buckets: dict[str, list[Candidate]] = defaultdict(list)
    for c in candidates:
        buckets[c.source].append(c)
    return buckets

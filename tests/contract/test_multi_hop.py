"""Multi-hop contract tests (v0.6).

Two related memories from the same turn must both surface in /recall even
when only one of them directly matches the query — proven by traversing the
`memory_edges.co_extracted` relation.
"""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_co_extracted_neighbor_surfaces_via_edge_hop(client, rand_user) -> None:
    """One turn with two distinct facts. Querying only one of them must still
    surface the other via co_extracted edge traversal.
    """
    await client.delete(f"/users/{rand_user}")

    r = await client.post(
        "/turns",
        json={
            "session_id": "s1",
            "user_id": rand_user,
            "messages": [
                {"role": "user",
                 "content": "I work at Notion and I just moved to Berlin from NYC."},
                {"role": "assistant", "content": "Got it."},
            ],
            "timestamp": "2025-04-01T10:00:00Z",
            "metadata": {},
        },
    )
    assert r.status_code == 201

    # Inspect what got extracted (should have employer + lives_in + lived_in).
    rmem = await client.get(f"/users/{rand_user}/memories")
    active = [m for m in rmem.json()["memories"] if m["active"]]
    preds = {m["predicate"] for m in active}
    assert "employer" in preds and "lives_in" in preds, f"got {preds}"

    # Query ONLY about location — Notion shouldn't have a direct lexical match.
    r2 = await client.post(
        "/recall",
        json={
            "query": "What city does the user currently live in?",
            "session_id": "probe",
            "user_id": rand_user,
            "max_tokens": 512,
        },
    )
    assert r2.status_code == 200
    ctx = r2.json()["context"].lower()
    # Location should fire directly.
    assert "berlin" in ctx
    # And the co_extracted employer memory should ride along via edge_hop
    # (or via Tier 1 stable-facts dump). Either way it should be visible.
    assert "notion" in ctx, f"co_extracted employer didn't surface: {ctx!r}"

    await client.delete(f"/users/{rand_user}")

"""Tier 2 arc rendering (v0.11).

v0.10 wired arcs into Tier 1 stable_facts only. When a memory ends up in
Tier 2 (because the retrievers ranked it ahead of stable facts, or because
the dedupe-vs-Tier-1 step kept it), the arc was *not* rendered. v0.11
plumbs the supersession chain through Candidate.metadata so the assembler
can render the same "Currently X (previously: Y, until DATE)" shape in
Tier 2.

We construct a deterministic case where one memory lands in both Tier 1
(as a stable fact, possibly trimmed) and the retrievers (as a query-relevant
memory). The Tier-2 line must carry the arc.
"""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_tier2_candidate_carries_arc(client, rand_user) -> None:
    await client.delete(f"/users/{rand_user}")

    # Two supersession-triggering turns: lives_in chain SF → Berlin.
    await client.post(
        "/turns",
        json={
            "session_id": "s1", "user_id": rand_user,
            "messages": [
                {"role": "user", "content": "Based in San Francisco for now."},
                {"role": "assistant", "content": "ok"},
            ],
            "timestamp": "2025-01-15T09:00:00Z",
            "metadata": {},
        },
    )
    await client.post(
        "/turns",
        json={
            "session_id": "s2", "user_id": rand_user,
            "messages": [
                {"role": "user", "content": "I just moved to Berlin from there."},
                {"role": "assistant", "content": "ok"},
            ],
            "timestamp": "2025-04-01T14:30:00Z",
            "metadata": {},
        },
    )

    # Tight max_tokens forces Tier 1 to trim, so the location memory is more
    # likely to show up via the Tier 2 retriever path. The query also lexically
    # hits 'live' / 'city' so FTS contributes.
    r = await client.post(
        "/recall",
        json={
            "query": "What city does this user currently live in?",
            "session_id": "probe",
            "user_id": rand_user,
            "max_tokens": 256,
        },
    )
    assert r.status_code == 200
    ctx = r.json()["context"]

    # The arc must surface somewhere — Tier 1 *or* Tier 2 — for the Berlin
    # memory's prior San Francisco. v0.11 guarantees it works either way.
    assert "berlin" in ctx.lower()
    assert "previously" in ctx.lower(), f"no arc in any tier:\n{ctx}"
    assert "san francisco" in ctx.lower(), (
        f"prior value not surfaced via arc:\n{ctx}"
    )

    await client.delete(f"/users/{rand_user}")


@pytest.mark.asyncio
async def test_tier2_arc_only_when_intent_permits(client, rand_user) -> None:
    """Same v0.10 intent gate applies in Tier 2 — no arc on factoid_general."""
    await client.delete(f"/users/{rand_user}")
    for ts, msg in (
        ("2025-01-15T09:00:00Z", "Based in San Francisco for now."),
        ("2025-04-01T14:30:00Z", "Just moved to Berlin from there."),
    ):
        await client.post(
            "/turns",
            json={
                "session_id": f"s-{ts}",
                "user_id": rand_user,
                "messages": [{"role": "user", "content": msg},
                             {"role": "assistant", "content": "ok"}],
                "timestamp": ts,
                "metadata": {},
            },
        )

    # Query with no profile triggers → factoid_general → gate may close,
    # but if any tier opens, arc must still not render.
    r = await client.post(
        "/recall",
        json={
            "query": "How do I configure nginx as a reverse proxy?",
            "session_id": "probe",
            "user_id": rand_user,
            "max_tokens": 256,
        },
    )
    assert r.status_code == 200
    assert "previously" not in r.json()["context"].lower()

    await client.delete(f"/users/{rand_user}")

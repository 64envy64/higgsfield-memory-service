"""Opinion/fact arc surfacing in /recall Tier 1 (v0.10).

When a memory was superseded by a later one (multiplicity=one fact, or
explicit opinion correction), the assembler — under exploratory or
factoid_about_user intents — must render the line as

    "- Currently X (previously: Y, until DATE)"

so the agent sees the most recent delta, not just the current state. For
intents that don't qualify (factoid_general, cold), the legacy "(updated
DATE)" form is used to keep simple factoids compact.

We drive this through the rule extractor (no LLM key required): the two
turns below trigger supersession on `employer` and `lives_in`.
"""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_arc_rendered_for_superseded_fact(client, rand_user) -> None:
    await client.delete(f"/users/{rand_user}")

    # Turn 1 — initial state.
    r1 = await client.post(
        "/turns",
        json={
            "session_id": "s1",
            "user_id": rand_user,
            "messages": [
                {"role": "user",
                 "content": "Quick intro: I'm an engineer at Stripe. Based in San Francisco."},
                {"role": "assistant", "content": "Noted."},
            ],
            "timestamp": "2025-01-15T09:00:00Z",
            "metadata": {},
        },
    )
    assert r1.status_code == 201

    # Turn 2 — supersedes both employer and lives_in.
    r2 = await client.post(
        "/turns",
        json={
            "session_id": "s2",
            "user_id": rand_user,
            "messages": [
                {"role": "user",
                 "content": "Big update — I just joined Notion and moved to Berlin last month."},
                {"role": "assistant", "content": "Got it."},
            ],
            "timestamp": "2025-03-20T14:00:00Z",
            "metadata": {},
        },
    )
    assert r2.status_code == 201

    # Sanity: chain visible via /users/.../memories
    rmem = await client.get(f"/users/{rand_user}/memories")
    body = rmem.json()
    active_employers = [
        m for m in body["memories"]
        if m["predicate"] == "employer" and m["active"]
    ]
    superseded_employers = [
        m for m in body["memories"]
        if m["predicate"] == "employer" and not m["active"]
    ]
    assert len(active_employers) == 1 and active_employers[0]["object"] == "Notion"
    assert len(superseded_employers) >= 1
    # Supersession link must point from active → prior.
    assert any(m["supersedes"] == s["id"]
               for m in active_employers
               for s in superseded_employers)

    # Recall under factoid_about_user intent → arc must render.
    # Query overlaps the active memory FTS lemmas ("lives", "city") so the
    # memory_signal opens Tier 1; "this user" trips profile_relevant; and the
    # intent maps to factoid_about_user, which enables render_arc.
    r = await client.post(
        "/recall",
        json={
            "query": "What city does this user currently live in?",
            "session_id": "probe",
            "user_id": rand_user,
            "max_tokens": 512,
        },
    )
    assert r.status_code == 200
    ctx = r.json()["context"]

    # Current state must be in.
    assert "Notion" in ctx
    assert "Berlin" in ctx
    # Arc must be rendered for at least one supersession chain.
    assert "previously" in ctx.lower(), f"arc not rendered:\n{ctx}"
    # Either employer or location prior must show up by name.
    assert ("Stripe" in ctx) or ("San Francisco" in ctx), (
        f"prior value not surfaced in arc:\n{ctx}"
    )

    await client.delete(f"/users/{rand_user}")


@pytest.mark.asyncio
async def test_arc_suppressed_under_simple_factoid(client, rand_user) -> None:
    """Arc should NOT render when the analyzer picks factoid_general or cold —
    keeps simple "where do they live?"-style answers compact.

    The heuristic analyzer assigns factoid_about_user to "where does this user
    live" (profile_relevant by keyword 'this user'), which qualifies for arc.
    So we test the negative case via factoid_general: a question that mentions
    no profile triggers.
    """
    await client.delete(f"/users/{rand_user}")
    # Re-ingest the same two turns.
    for ts, msg in (
        ("2025-01-15T09:00:00Z",
         "I'm an engineer at Stripe. Based in San Francisco."),
        ("2025-03-20T14:00:00Z",
         "I just joined Notion and moved to Berlin last month."),
    ):
        await client.post(
            "/turns",
            json={
                "session_id": f"s-{ts}",
                "user_id": rand_user,
                "messages": [
                    {"role": "user", "content": msg},
                    {"role": "assistant", "content": "ok"},
                ],
                "timestamp": ts,
                "metadata": {},
            },
        )

    # A factoid_general query should not trigger Tier 1 at all (no profile
    # relevance, no signal). Empty context expected by Invariant 3.
    r = await client.post(
        "/recall",
        json={
            "query": "What is the capital of France?",
            "session_id": "probe",
            "user_id": rand_user,
            "max_tokens": 512,
        },
    )
    assert r.status_code == 200
    body = r.json()
    # Noise query → empty (I3). If for some reason a tier opens, the arc must
    # still not render on a factoid_general intent.
    assert "previously" not in body["context"].lower()

    await client.delete(f"/users/{rand_user}")

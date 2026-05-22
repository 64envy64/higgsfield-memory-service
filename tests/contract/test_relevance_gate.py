"""Relevance gate + budget contract tests (v0.8).

These pin Invariant 3 ("empty over wrong") to behaviour the eval can probe.
"""
from __future__ import annotations

import pytest


async def _ingest(client, user_id: str, session: str, text: str, ts: str = "2025-03-15T10:30:00Z") -> None:
    r = await client.post(
        "/turns",
        json={
            "session_id": session,
            "user_id": user_id,
            "messages": [
                {"role": "user", "content": text},
                {"role": "assistant", "content": "Got it."},
            ],
            "timestamp": ts,
            "metadata": {},
        },
    )
    assert r.status_code == 201, r.text


async def _recall(client, user_id: str, q: str, max_tokens: int = 512) -> dict:
    r = await client.post(
        "/recall",
        json={"query": q, "session_id": f"probe-{user_id}", "user_id": user_id, "max_tokens": max_tokens},
    )
    assert r.status_code == 200
    return r.json()


@pytest.mark.asyncio
async def test_irrelevant_query_returns_empty(client, rand_user) -> None:
    """A query about something the user never discussed must not dump profile facts."""
    await client.delete(f"/users/{rand_user}")
    await _ingest(client, rand_user, "s1", "I work at Stripe in San Francisco.")
    await _ingest(client, rand_user, "s2", "Walking Biscuit before work today.")

    # Pure noise queries:
    for q in ["What is the capital of France?", "How do I configure nginx?", "Explain quicksort"]:
        body = await _recall(client, rand_user, q)
        assert body["context"] == "", f"non-empty context for noise query {q!r}: {body['context']!r}"
        assert body["citations"] == []

    await client.delete(f"/users/{rand_user}")


@pytest.mark.asyncio
async def test_anonymous_session_roundtrip(client, rand_session) -> None:
    """user_id=null falls back to session scope. Memories don't leak to other sessions."""
    r = await client.post(
        "/turns",
        json={
            "session_id": rand_session,
            "user_id": None,
            "messages": [
                {"role": "user", "content": "I love dark roast coffee."},
                {"role": "assistant", "content": "Noted."},
            ],
            "timestamp": "2025-03-15T10:30:00Z",
            "metadata": {},
        },
    )
    assert r.status_code == 201

    # Same session: can recall.
    body = await _recall(client, "should-not-be-used", q="Tell me about coffee preferences")
    # When user_id is provided as a string, scope is user — and this user has no data.
    # So the cross-pollination case is correctly empty.
    assert body["context"] == ""

    # Querying the same session_id with user_id=null returns the session-scope memories.
    r = await client.post(
        "/recall",
        json={"query": "coffee preferences", "session_id": rand_session, "user_id": None, "max_tokens": 256},
    )
    assert r.status_code == 200
    body = r.json()
    assert "coffee" in body["context"].lower() or "dark roast" in body["context"].lower()

    await client.delete(f"/sessions/{rand_session}")


@pytest.mark.asyncio
async def test_tight_token_budget_is_respected(client, rand_user) -> None:
    """Hard test: max_tokens=50. Context must not blow past it materially."""
    await client.delete(f"/users/{rand_user}")
    for i in range(5):
        await _ingest(
            client, rand_user, f"s{i}",
            f"Fact number {i}: I really really really really love widgets of type {i}.",
            ts=f"2025-03-{15 + i:02d}T10:30:00Z",
        )

    body = await _recall(client, rand_user, "Tell me about my widget preferences", max_tokens=50)
    # Token count via tiktoken (cl100k) should be within 1.5x the budget — assembler
    # is best-effort, not byte-exact, but should not double the budget.
    import tiktoken
    enc = tiktoken.get_encoding("cl100k_base")
    n_tok = len(enc.encode(body["context"]))
    assert n_tok <= 80, f"context far exceeds budget: {n_tok} tokens for max_tokens=50"

    await client.delete(f"/users/{rand_user}")


@pytest.mark.asyncio
async def test_cold_user_returns_empty(client, rand_user) -> None:
    """Even profile-relevant query on a brand-new user → empty."""
    body = await _recall(client, rand_user, "What do you remember about me?")
    assert body["context"] == ""
    assert body["citations"] == []

"""v0.1 shape-only contract tests.

These verify that every endpoint exists, accepts the spec'd payloads, and
returns the spec'd shape. Behaviour (real extraction, retrieval, supersession)
is exercised by later test modules added as the corresponding features land.
"""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_turn_roundtrip_shape(client, rand_user, rand_session) -> None:
    payload = {
        "session_id": rand_session,
        "user_id": rand_user,
        "messages": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ],
        "timestamp": "2025-03-15T10:30:00Z",
        "metadata": {},
    }
    r = await client.post("/turns", json=payload)
    assert r.status_code == 201
    body = r.json()
    assert "id" in body and isinstance(body["id"], str) and len(body["id"]) > 0


@pytest.mark.asyncio
async def test_recall_shape(client, rand_user, rand_session) -> None:
    r = await client.post(
        "/recall",
        json={
            "query": "where do they live?",
            "session_id": rand_session,
            "user_id": rand_user,
            "max_tokens": 256,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert "context" in body and isinstance(body["context"], str)
    assert "citations" in body and isinstance(body["citations"], list)


@pytest.mark.asyncio
async def test_recall_cold_session_returns_empty(client, rand_user, rand_session) -> None:
    r = await client.post(
        "/recall",
        json={
            "query": "anything",
            "session_id": rand_session,
            "user_id": rand_user,
            "max_tokens": 128,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["context"] == ""
    assert body["citations"] == []


@pytest.mark.asyncio
async def test_search_shape(client, rand_user) -> None:
    r = await client.post(
        "/search",
        json={"query": "anything", "user_id": rand_user, "limit": 5},
    )
    assert r.status_code == 200
    body = r.json()
    assert "results" in body and isinstance(body["results"], list)


@pytest.mark.asyncio
async def test_search_both_scopes_null_returns_empty(client) -> None:
    """When both user_id and session_id are null, no global search."""
    r = await client.post(
        "/search",
        json={"query": "anything", "limit": 5},
    )
    assert r.status_code == 200
    assert r.json() == {"results": []}


@pytest.mark.asyncio
async def test_user_memories_shape(client, rand_user) -> None:
    r = await client.get(f"/users/{rand_user}/memories")
    assert r.status_code == 200
    body = r.json()
    assert "memories" in body and isinstance(body["memories"], list)


@pytest.mark.asyncio
async def test_delete_session_idempotent(client, rand_session) -> None:
    r = await client.delete(f"/sessions/{rand_session}")
    assert r.status_code == 204
    # Calling twice is fine — still 204.
    r2 = await client.delete(f"/sessions/{rand_session}")
    assert r2.status_code == 204


@pytest.mark.asyncio
async def test_delete_user_idempotent(client, rand_user) -> None:
    r = await client.delete(f"/users/{rand_user}")
    assert r.status_code == 204


@pytest.mark.asyncio
async def test_malformed_json_returns_422(client) -> None:
    r = await client.post(
        "/turns",
        content=b"{not valid json",
        headers={"Content-Type": "application/json"},
    )
    # FastAPI returns 422 on validation/json errors.
    assert r.status_code in (400, 422)
    body = r.json()
    assert "detail" in body or "error" in body


@pytest.mark.asyncio
async def test_missing_required_field_returns_422(client) -> None:
    r = await client.post(
        "/turns",
        json={"session_id": "s1"},  # missing messages, timestamp
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_unicode_payload_does_not_crash(client, rand_user, rand_session) -> None:
    payload = {
        "session_id": rand_session,
        "user_id": rand_user,
        "messages": [
            {"role": "user", "content": "Привет 👋 我喜欢咖啡 ‮mirror‬"},
            {"role": "assistant", "content": "🚀✅"},
        ],
        "timestamp": "2025-03-15T10:30:00Z",
        "metadata": {"locale": "ru-RU"},
    }
    r = await client.post("/turns", json=payload)
    assert r.status_code == 201

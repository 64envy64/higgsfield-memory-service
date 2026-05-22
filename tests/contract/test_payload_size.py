"""Body size limit contract test (v0.9.1).

The middleware rejects oversized bodies with 413 before Pydantic gets a chance
to allocate them. Sub-limit payloads must still pass through normally.
"""
from __future__ import annotations

import json

import pytest


@pytest.mark.asyncio
async def test_oversized_body_rejected_with_413(client, rand_user, rand_session) -> None:
    # 1 MiB of payload — well above the 512KiB default cap.
    huge_content = "x" * (1 * 1024 * 1024)
    payload = {
        "session_id": rand_session,
        "user_id": rand_user,
        "messages": [
            {"role": "user", "content": huge_content},
            {"role": "assistant", "content": "ok"},
        ],
        "timestamp": "2025-03-15T10:30:00Z",
        "metadata": {},
    }
    r = await client.post("/turns", json=payload)
    assert r.status_code == 413, f"expected 413, got {r.status_code}: {r.text}"
    body = r.json()
    assert body.get("error") == "payload_too_large"


@pytest.mark.asyncio
async def test_normal_body_passes_through(client, rand_user, rand_session) -> None:
    """A small body must still succeed — middleware is a guard, not a wall."""
    r = await client.post(
        "/turns",
        json={
            "session_id": rand_session,
            "user_id": rand_user,
            "messages": [
                {"role": "user", "content": "small ordinary payload"},
                {"role": "assistant", "content": "ok"},
            ],
            "timestamp": "2025-03-15T10:30:00Z",
            "metadata": {},
        },
    )
    assert r.status_code == 201
    await client.delete(f"/users/{rand_user}")


@pytest.mark.asyncio
async def test_invalid_content_length_rejected(client) -> None:
    r = await client.post(
        "/turns",
        content=json.dumps({"session_id": "s", "messages": [], "timestamp": "2025-03-15T10:30:00Z"}),
        headers={"Content-Type": "application/json", "Content-Length": "not-a-number"},
    )
    # Either the middleware rejects with 400 (preferred) or the server's parser
    # rejects with 400/422 — anything in the 4xx range with no crash is OK.
    assert 400 <= r.status_code < 500

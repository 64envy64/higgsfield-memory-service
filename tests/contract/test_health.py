from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_health_ok(client) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"


@pytest.mark.asyncio
async def test_health_does_not_require_auth(monkeypatch, client) -> None:
    """Even when an auth token is configured, /health must remain public."""
    from memory_service.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "auth_token", "test-secret-token")

    # No Authorization header on this request.
    r = await client.get("/health")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_protected_endpoint_requires_auth_when_token_set(
    monkeypatch, client, rand_user
) -> None:
    """When auth is configured, contract endpoints reject unauthenticated calls with 401."""
    from memory_service.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "auth_token", "test-secret-token")

    r = await client.get(f"/users/{rand_user}/memories")
    assert r.status_code == 401

    # With the correct token, it works.
    r2 = await client.get(
        f"/users/{rand_user}/memories",
        headers={"Authorization": "Bearer test-secret-token"},
    )
    assert r2.status_code == 200


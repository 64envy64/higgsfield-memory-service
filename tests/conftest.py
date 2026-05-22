from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest

# Make sure the in-process app talks to the test database. When the suite is
# launched inside the docker container, MEMORY_DATABASE_URL is already set.
os.environ.setdefault(
    "MEMORY_DATABASE_URL", "postgresql://memory:memory@db:5432/memory"
)


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
async def app():
    """Import once. Lifespan is managed by the client fixture."""
    from memory_service.main import app as fastapi_app
    return fastapi_app


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    """Httpx client that drives the ASGI app, with proper lifespan startup/shutdown."""
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://memtest",
            timeout=30.0,
        ) as ac:
            yield ac


@pytest.fixture
def rand_user() -> str:
    return f"user-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def rand_session() -> str:
    return f"sess-{uuid.uuid4().hex[:8]}"

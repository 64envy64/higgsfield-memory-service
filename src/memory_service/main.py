from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from memory_service import __version__
from memory_service.api import (
    routes_admin,
    routes_memories,
    routes_recall,
    routes_search,
    routes_turns,
)
from memory_service.api.middleware import PayloadSizeLimitMiddleware
from memory_service.config import get_settings
from memory_service.db.pool import create_pool, run_migrations
from memory_service.util.logging import configure_logging

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    logger.info("memory-service %s starting", __version__)

    pool = await create_pool(settings.database_url)
    await run_migrations(pool)
    app.state.pool = pool

    if not settings.llm_enabled:
        logger.warning(
            "OPENAI_API_KEY is not set — running in lexical-only mode. "
            "Semantic retrieval and nuanced extraction will degrade."
        )

    try:
        yield
    finally:
        logger.info("memory-service shutting down")
        await pool.close()


app = FastAPI(
    title="memory-service",
    version=__version__,
    lifespan=lifespan,
)

# Enforce body-size limit BEFORE Pydantic gets a chance to parse a multi-MB blob.
# Settings is read once at module import time — same singleton as the routes use.
_settings = get_settings()
app.add_middleware(PayloadSizeLimitMiddleware, max_bytes=_settings.max_payload_bytes)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Convert Pydantic errors to a stable 422 shape — never crash, never 500."""
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": exc.errors(), "error": "validation_error"},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort handler — log + return 500 without leaking a stack trace."""
    logger.exception("unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": "internal_error", "detail": "request failed"},
    )


# Admin (/health, DELETE /sessions, DELETE /users)
app.include_router(routes_admin.router, tags=["admin"])
# Contract endpoints
app.include_router(routes_turns.router, tags=["turns"])
app.include_router(routes_recall.router, tags=["recall"])
app.include_router(routes_search.router, tags=["search"])
app.include_router(routes_memories.router, tags=["memories"])


@app.get("/", include_in_schema=False)
async def root() -> dict[str, Any]:
    return {"service": "memory-service", "version": __version__}

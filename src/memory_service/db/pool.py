from __future__ import annotations

import json
import logging
from pathlib import Path

import asyncpg

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


async def create_pool(dsn: str) -> asyncpg.Pool:
    """Create the asyncpg pool. Caller is responsible for closing it."""
    pool = await asyncpg.create_pool(
        dsn=dsn,
        min_size=1,
        max_size=10,
        command_timeout=30,
        timeout=10,
        # Register vector type once per connection so we can pass embeddings as lists.
        init=_init_connection,
    )
    return pool


async def _init_connection(conn: asyncpg.Connection) -> None:
    # JSONB / JSON arrive as text by default; decode to native dicts so callers
    # don't have to remember json.loads() at every fetch site.
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog",
    )
    await conn.set_type_codec(
        "json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog",
    )
    # vector is shipped as text by pgvector unless a codec is registered; we cast
    # on the SQL side (::vector) so no codec is strictly required.


async def run_migrations(pool: asyncpg.Pool) -> None:
    """Apply SQL migrations in lexical order. Idempotent; safe on restart."""
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        logger.warning("no migrations found at %s", MIGRATIONS_DIR)
        return
    async with pool.acquire() as conn:
        for f in files:
            sql = f.read_text(encoding="utf-8")
            logger.info("applying migration %s", f.name)
            # Each migration is wrapped in a transaction.
            async with conn.transaction():
                await conn.execute(sql)
    logger.info("migrations applied")

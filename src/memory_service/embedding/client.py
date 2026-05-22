"""Embedding client.

A thin wrapper around OpenAI's embeddings API with a hard feature-flag fallback:
when OPENAI_API_KEY is absent, `embed()` returns None and `is_enabled` is False.
Callers must handle the None branch by leaving the embedding column NULL —
lexical retrieval then carries the load. We deliberately do not pad or hash
into a fake 1536-d vector: pseudo-semantics would be worse than honest None.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable

from openai import AsyncOpenAI
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from memory_service.config import Settings

logger = logging.getLogger(__name__)


class EmbeddingClient:
    """OpenAI text-embedding-3-small (1536d). Async, retry-wrapped, timeout-bounded."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: AsyncOpenAI | None = None
        if settings.llm_enabled:
            self._client = AsyncOpenAI(
                api_key=settings.openai_api_key,
                timeout=15.0,
                max_retries=0,  # we manage retries via tenacity below
            )

    @property
    def is_enabled(self) -> bool:
        return self._client is not None

    @property
    def dim(self) -> int:
        return self._settings.embedding_dim

    async def embed(self, text: str) -> list[float] | None:
        """Embed a single piece of text. Returns None if disabled or on persistent failure."""
        if self._client is None:
            return None
        text = (text or "").strip()
        if not text:
            return None

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(2),
                wait=wait_exponential(multiplier=0.3, min=0.3, max=2.0),
                retry=retry_if_exception_type((TimeoutError, ConnectionError)),
                reraise=True,
            ):
                with attempt:
                    resp = await asyncio.wait_for(
                        self._client.embeddings.create(
                            model=self._settings.embedding_model,
                            input=text[:8000],  # text-embedding-3-* cap is 8192 tokens
                        ),
                        timeout=8.0,
                    )
                    return list(resp.data[0].embedding)
        except Exception as e:                                  # broad: also catches RateLimit, BadRequest
            logger.warning("embed failed (%s): %s", type(e).__name__, e)
            return None

    async def embed_batch(self, texts: Iterable[str]) -> list[list[float] | None]:
        """Best-effort batch embed. One element per input; per-element fallback to None on failure."""
        texts = list(texts)
        if not texts:
            return []
        if self._client is None:
            return [None] * len(texts)
        cleaned = [(t or "").strip()[:8000] for t in texts]
        non_empty_idx = [i for i, t in enumerate(cleaned) if t]
        if not non_empty_idx:
            return [None] * len(texts)
        try:
            resp = await asyncio.wait_for(
                self._client.embeddings.create(
                    model=self._settings.embedding_model,
                    input=[cleaned[i] for i in non_empty_idx],
                ),
                timeout=10.0,
            )
            out: list[list[float] | None] = [None] * len(texts)
            for vec, i in zip(resp.data, non_empty_idx, strict=True):
                out[i] = list(vec.embedding)
            return out
        except Exception as e:
            logger.warning("embed_batch failed (%s): %s", type(e).__name__, e)
            return [None] * len(texts)


_singleton: EmbeddingClient | None = None


def get_embedding_client(settings: Settings) -> EmbeddingClient:
    global _singleton
    if _singleton is None:
        _singleton = EmbeddingClient(settings)
    return _singleton


def reset_embedding_client() -> None:
    """For tests: drop the cached client so the next get_*() rebuilds with current settings."""
    global _singleton
    _singleton = None

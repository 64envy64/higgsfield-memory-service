"""LLM client.

Wraps OpenAI chat completions. Same is_enabled discipline as the embedding client.
Exposes `chat_json()` — a constrained chat call expected to return parseable JSON.

When disabled the method returns None; callers fall back to rule-based or
RRF-only paths. No fake completions, ever.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

from memory_service.config import Settings

logger = logging.getLogger(__name__)


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: AsyncOpenAI | None = None
        if settings.llm_enabled:
            self._client = AsyncOpenAI(
                api_key=settings.openai_api_key,
                timeout=30.0,
                max_retries=0,
            )

    @property
    def is_enabled(self) -> bool:
        return self._client is not None

    async def chat_json(
        self,
        *,
        model: str | None = None,
        system: str,
        user: str,
        timeout_s: float = 25.0,
        temperature: float = 0.0,
    ) -> dict[str, Any] | None:
        """Chat call constrained to JSON object output. Returns parsed dict or None."""
        if self._client is None:
            return None
        try:
            messages: list[ChatCompletionMessageParam] = [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]
            resp = await asyncio.wait_for(
                self._client.chat.completions.create(
                    model=model or self._settings.extraction_model,
                    messages=messages,
                    response_format={"type": "json_object"},
                    temperature=temperature,
                ),
                timeout=timeout_s,
            )
            content = resp.choices[0].message.content or ""
            if not content:
                return None
            return json.loads(content)
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning("chat_json timeout after %.1fs", timeout_s)
            return None
        except json.JSONDecodeError as e:
            logger.warning("chat_json: response was not valid JSON: %s", e)
            return None
        except Exception as e:
            logger.warning("chat_json failed (%s): %s", type(e).__name__, e)
            return None

_singleton: LLMClient | None = None


def get_llm_client(settings: Settings) -> LLMClient:
    global _singleton
    if _singleton is None:
        _singleton = LLMClient(settings)
    return _singleton


def reset_llm_client() -> None:
    global _singleton
    _singleton = None

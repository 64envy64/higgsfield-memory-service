"""ExtractionService: orchestrates LLM + rule extraction.

The service-level contract is `extract(messages_text) -> list[MemoryCandidate]`.
LLM is preferred; rule-based is the fallback when the LLM is disabled or
returned nothing useful.
"""
from __future__ import annotations

import logging

from memory_service.config import Settings
from memory_service.extraction import llm_extractor, rule_extractor
from memory_service.extraction.models import MemoryCandidate
from memory_service.llm.client import LLMClient

logger = logging.getLogger(__name__)


class ExtractionService:
    def __init__(self, *, settings: Settings, llm: LLMClient) -> None:
        self._settings = settings
        self._llm = llm

    async def extract(self, messages_text: str) -> list[MemoryCandidate]:
        if not messages_text.strip():
            return []

        if self._llm.is_enabled:
            cands = await llm_extractor.extract_via_llm(
                client=self._llm,
                messages_text=messages_text,
                timeout_s=self._settings.extraction_timeout_s,
            )
            if cands:
                logger.info("extracted %d candidates via LLM", len(cands))
                return cands
            # LLM returned nothing — could be a genuine empty turn or a silent
            # failure. We fall back to rules so we don't lose easy wins.
            logger.info("LLM returned no candidates — trying rule-based fallback")

        cands = rule_extractor.extract_via_rules(messages_text)
        logger.info("extracted %d candidates via rules", len(cands))
        return cands


_singleton: ExtractionService | None = None


def get_extraction_service(settings: Settings, llm: LLMClient) -> ExtractionService:
    global _singleton
    if _singleton is None:
        _singleton = ExtractionService(settings=settings, llm=llm)
    return _singleton


def reset_extraction_service() -> None:
    global _singleton
    _singleton = None

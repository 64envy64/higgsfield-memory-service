"""LLM-based extractor.

Single prompt → JSON object → list of memory candidates + entity mentions.
We use OpenAI `response_format={"type":"json_object"}` so the output is always
parseable JSON; structured-outputs validation happens in Python (Pydantic).

Failure modes (all → return []):
  * LLM unreachable / no key → caller falls back to the rule-based extractor.
  * LLM returns invalid JSON → swallowed by `LLMClient.chat_json`.
  * JSON doesn't match the expected envelope → returns [].
  * Individual candidate missing required fields → dropped, others kept.
"""
from __future__ import annotations

import logging
from typing import Any

from memory_service.extraction.models import EntityMention, MemoryCandidate
from memory_service.extraction.taxonomy import normalize_predicate, predicates_prompt_block
from memory_service.llm.client import LLMClient

logger = logging.getLogger(__name__)


SYSTEM = """\
You extract durable, queryable knowledge from a single conversation turn.

Your job is to surface facts, preferences, opinions, and notable events about
the USER (the human in the conversation), so they can be recalled in future
sessions. You do NOT extract things about the assistant, and you do NOT invent
facts that aren't stated.

Output rules:
- Return JSON: {"memories": [...], "entities": [...]}.
- `memories` is a list of objects with fields:
    type      : "fact" | "preference" | "opinion" | "event"
    subject   : almost always "user". For facts about a named non-user entity
                (e.g. a pet) use "<type>:<name>", e.g. "pet:Biscuit".
    predicate : prefer one from the list below; otherwise "other:<topic>".
    object    : the canonical value (e.g. "Notion", "Berlin", "shellfish",
                "TypeScript"). Short, normalized.
    value     : human-readable summary ("Works at Notion as a PM").
    raw_quote : the verbatim phrase from the user message it came from.
    confidence: 0.0–1.0. Lower confidence if it's hedged ("I think", "maybe").
- `entities` is a list of {name, type} for named entities mentioned
  (people, pets, places, orgs). Include every distinct named entity that the
  memories reference, so multi-hop recall can traverse later.

What to extract:
- Personal facts: employer, job title, location (current and past),
  family, pets, dietary needs, allergies.
- Preferences and opinions, including hedged ones.
- IMPLICIT facts: "walking Biscuit before work" → owns_pet/pet_name = Biscuit;
  "had to leave early to pick up the kids" → has children.
- CORRECTIONS: when the user says "actually, …" or "sorry, I meant …",
  emit the corrected fact at higher confidence. The reconciler will handle
  supersession; you only need to emit the new state.

What NOT to extract:
- Anything stated by the assistant (assistant turns are context only).
- Hypotheticals ("if I moved to Berlin…") unless they're stating intent.
- Generic small talk with no durable content.

If the turn has nothing extractable, return {"memories": [], "entities": []}.

"""


def _build_user_prompt(messages_text: str) -> str:
    return (
        predicates_prompt_block()
        + "\n\n"
        + "TURN TO EXTRACT FROM:\n"
        + "----------------------------------------\n"
        + messages_text.strip()
        + "\n----------------------------------------\n"
    )


async def extract_via_llm(
    *,
    client: LLMClient,
    messages_text: str,
    timeout_s: float = 30.0,
) -> list[MemoryCandidate]:
    """Run one LLM extraction over the joined turn text. Returns [] on any failure."""
    if not client.is_enabled:
        return []

    user_prompt = _build_user_prompt(messages_text)
    obj = await client.chat_json(
        system=SYSTEM,
        user=user_prompt,
        timeout_s=timeout_s,
        temperature=0.0,
    )
    if not obj:
        return []

    raw_memories = obj.get("memories", []) or []
    raw_entities = obj.get("entities", []) or []
    if not isinstance(raw_memories, list):
        logger.warning("extractor returned non-list memories: %s", type(raw_memories).__name__)
        return []

    # Normalize the entity list once; we'll attach to each candidate.
    entity_objs: list[EntityMention] = []
    if isinstance(raw_entities, list):
        for e in raw_entities:
            if isinstance(e, dict) and e.get("name"):
                entity_objs.append(EntityMention(
                    name=str(e["name"]).strip(),
                    type=(str(e["type"]).strip().lower() if e.get("type") else None),
                ))

    out: list[MemoryCandidate] = []
    for m in raw_memories:
        if not isinstance(m, dict):
            continue
        try:
            mtype = str(m.get("type", "fact")).lower()
            if mtype not in ("fact", "preference", "opinion", "event"):
                mtype = "fact"
            subject = str(m.get("subject", "user")).strip() or "user"
            predicate = normalize_predicate(str(m.get("predicate", "")))
            obj_v = str(m.get("object", "")).strip()
            value = str(m.get("value", "")).strip()
            raw_quote = str(m.get("raw_quote", "")).strip()
            confidence = float(m.get("confidence", 0.7))
            if not value or not obj_v:
                continue
            confidence = max(0.0, min(1.0, confidence))
            # Per-candidate entities: union of envelope entities with anything embedded.
            local_ents = list(entity_objs)
            for sub_e in (m.get("entities") or []):
                if isinstance(sub_e, dict) and sub_e.get("name"):
                    local_ents.append(EntityMention(
                        name=str(sub_e["name"]).strip(),
                        type=(str(sub_e["type"]).strip().lower() if sub_e.get("type") else None),
                    ))
            out.append(MemoryCandidate(
                type=mtype,                                     # type: ignore[arg-type]
                subject=subject,
                predicate=predicate,
                object=obj_v,
                value=value,
                raw_quote=raw_quote,
                confidence=confidence,
                entities=local_ents,
            ))
        except (TypeError, ValueError) as e:
            logger.warning("extractor: dropping malformed candidate: %s", e)
            continue

    return out

"""QueryAnalyzer.

One LLM call classifies the query and surfaces entities. The fallback (when
LLM is disabled or fails) is a deterministic heuristic: keyword-based intent
detection and naive entity extraction (capitalized tokens).

Output:
  intent            — coarse category, used for assembler priorities
  profile_relevant  — should we include stable user-facts? (Tier 1 gate)
  entities          — proper-noun-like tokens for graph-hop expansion (v0.6)
  expanded_queries  — paraphrases that broaden FTS / vector coverage
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Literal

from memory_service.llm.client import LLMClient

logger = logging.getLogger(__name__)

Intent = Literal["factoid_about_user", "factoid_general", "exploratory", "recent_context", "cold"]


@dataclass
class QueryAnalysis:
    intent: Intent
    profile_relevant: bool
    entities: list[str] = field(default_factory=list)
    expanded_queries: list[str] = field(default_factory=list)


SYSTEM = """\
You classify a single recall query that an agent is about to ask its memory store.

Return JSON with this exact shape:
{
  "intent": "factoid_about_user" | "factoid_general" | "exploratory" | "recent_context" | "cold",
  "profile_relevant": true | false,
  "entities": ["..."],
  "expanded_queries": ["...", "..."]
}

Definitions:
- intent:
    factoid_about_user  — asks about the user's own facts/preferences/history
    factoid_general     — asks about something outside the user's life
                          (general knowledge, code, the world)
    exploratory         — open-ended ("what should I cook tonight given my prefs?")
    recent_context      — refers explicitly to the current/recent conversation
                          ("what did I just say about X?")
    cold                — small talk / greetings / nothing to recall

- profile_relevant:
    true  iff returning stable facts about the user (job, location, pets, allergies,
          preferences, recent activities) would help answer this query.
    false otherwise. False for factoid_general and cold.

- entities: proper-noun-like things in the query (names, places, orgs, pets).
            Used to traverse the user's knowledge graph. Lowercased not required.

- expanded_queries: 0–2 short paraphrases that preserve meaning. Used to
                    broaden lexical retrieval. Skip if the original is already concise.

Be strict: if the query is obviously unrelated to the user, return profile_relevant=false.
"""


_PROFILE_TRIGGERS = re.compile(
    r"\b("
    r"my|mine|i\b|me\b|user|users?|"
    r"prefer|preferences?|like|likes?|love|hate|dislike|"
    r"remember|recall|know\s+about|"
    r"work|job|employer|company|"
    r"live|location|city|address|"
    r"pet|dog|cat|family|partner|wife|husband|kids|"
    r"allerg|diet|vegetarian|vegan|"
    r"hobby|hobbies"
    r")\b",
    re.IGNORECASE,
)

_CAP_TOKEN = re.compile(r"\b([A-Z][\w'-]+(?:\s+[A-Z][\w'-]+){0,2})\b")
_NEGATIVE_INTENT = re.compile(
    r"\b(capital|nginx|kubernetes|sql|python|javascript|api|http|how\s+to|"
    r"what\s+is\s+(the|a)\b|why\s+is)\b",
    re.IGNORECASE,
)


def _heuristic(query: str) -> QueryAnalysis:
    """No-LLM fallback. Cheap, deterministic, conservative."""
    profile = bool(_PROFILE_TRIGGERS.search(query))
    intent: Intent
    if not profile:
        intent = "factoid_general" if _NEGATIVE_INTENT.search(query) else "exploratory"
    elif "remember" in query.lower() or "recall" in query.lower() or "just" in query.lower():
        intent = "recent_context"
    else:
        intent = "factoid_about_user"

    # Naive entity extraction: capitalized 1–3 word spans.
    entities = []
    for m in _CAP_TOKEN.finditer(query):
        token = m.group(1).strip()
        if token and token.lower() not in {"what", "where", "when", "who", "why", "how", "the"}:
            entities.append(token)

    return QueryAnalysis(
        intent=intent,
        profile_relevant=profile,
        entities=entities[:6],
        expanded_queries=[],
    )


async def analyze(
    query: str,
    *,
    llm: LLMClient,
    timeout_s: float = 6.0,
) -> QueryAnalysis:
    """Analyze a recall query. LLM path → heuristic fallback on any failure."""
    if not query.strip():
        return QueryAnalysis(intent="cold", profile_relevant=False)

    if llm.is_enabled:
        obj = await llm.chat_json(
            system=SYSTEM,
            user=f"QUERY: {query.strip()}",
            timeout_s=timeout_s,
            temperature=0.0,
        )
        if obj:
            try:
                intent_raw = str(obj.get("intent", "")).lower()
                if intent_raw not in (
                    "factoid_about_user", "factoid_general",
                    "exploratory", "recent_context", "cold",
                ):
                    intent_raw = "exploratory"
                ents = obj.get("entities", []) or []
                xq = obj.get("expanded_queries", []) or []
                return QueryAnalysis(
                    intent=intent_raw,                                      # type: ignore[arg-type]
                    profile_relevant=bool(obj.get("profile_relevant", False)),
                    entities=[str(e).strip() for e in ents if str(e).strip()][:8],
                    expanded_queries=[str(q).strip() for q in xq if str(q).strip()][:3],
                )
            except (TypeError, ValueError) as e:
                logger.warning("QueryAnalyzer: malformed LLM JSON: %s", e)

    return _heuristic(query)

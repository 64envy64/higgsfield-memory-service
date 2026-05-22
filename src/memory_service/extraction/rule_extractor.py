"""Rule-based fallback extractor.

When OPENAI_API_KEY is missing the LLM extractor is a no-op and we fall back
here. The intent is *not* to match LLM quality — it's to keep the data-model
machinery (typed memories, supersession, multi-hop) measurable end-to-end in
the absence of a key. We cover the most common patterns and explicitly accept
poor recall on the long tail.

Patterns covered (each emits a `MemoryCandidate` from a user-role message):
  * employer / current job:       "I work at X", "I just joined X (as Y)",
                                  "I'm a/an Y at X"
  * job title:                    "I'm a/an Y" (when no employer follows)
  * location:                     "I live in X", "I moved to X", "based in X"
  * past location:                "I'm from X", "I used to live in X"
  * pet ownership / name:         "my dog X", "walking X" (heuristic on capitalized name)
  * dietary:                      "I'm vegetarian/vegan", "I don't eat X"
  * allergy:                      "I'm allergic to X"
  * preference (likes/dislikes):  "I love/like/enjoy X", "I hate/dislike X"
  * opinion:                      "TypeScript/Python/… is great/fine/awful for …"
  * correction:                   "actually, … X" / "sorry, I meant X"
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable

from memory_service.extraction.models import EntityMention, MemoryCandidate

logger = logging.getLogger(__name__)


# Capitalized place / org / pet name (1–3 words). Approximate, not perfect.
# We wrap in (?-i:...) so the [A-Z] anchor stays case-sensitive even when the
# enclosing pattern is matched with re.IGNORECASE — otherwise "Notion last month"
# greedily eats trailing lowercase words.
_CAP = r"(?-i:[A-Z][\w'-]+(?:\s+[A-Z][\w'-]+){0,2})"


PATTERNS: tuple[tuple[str, str, str, str], ...] = (
    # (regex, predicate, type, object_group_name_or_literal)
    # Employment ------------------------------------------------------------
    (rf"\bi(?:'m| am)?\s+(?:an?|the)\s+(\w+(?:\s+\w+){{0,2}})\s+at\s+({_CAP})\b",
     "employer", "fact", "g2"),
    (rf"\bi(?:'ve)?\s+just\s+joined\s+({_CAP})(?:\s+as\s+an?\s+(\w+(?:\s+\w+){{0,2}}))?",
     "employer", "fact", "g1"),
    (rf"\bi\s+work(?:ed|ing)?\s+(?:at|for)\s+({_CAP})\b",
     "employer", "fact", "g1"),
    (rf"\bi\s+joined\s+({_CAP})\b",
     "employer", "fact", "g1"),
    # Location --------------------------------------------------------------
    # Allow adverbs / linking words between "I" and "moved" — "I just moved",
    # "and moved to X", "I recently moved to X".
    (rf"\b(?:i|and)\s+(?:just\s+|recently\s+|finally\s+|already\s+|then\s+)?"
     rf"(?:moved|relocated|relocating)\s+(?:to|out\s+to)\s+({_CAP})\b",
     "lives_in", "fact", "g1"),
    (rf"\bi\s+(?:live|am\s+living)\s+in\s+({_CAP})\b",
     "lives_in", "fact", "g1"),
    (rf"\bbased\s+in\s+({_CAP})\b",
     "lives_in", "fact", "g1"),
    (rf"\bi\s+(?:used\s+to\s+live|lived)\s+in\s+({_CAP})\b",
     "lived_in", "fact", "g1"),
    # "moved … from X" or "from X" in a relocation sentence ⇒ previous city.
    (rf"\b(?:moved|relocated|moving)\s+(?:to\s+\S+\s+)?from\s+({_CAP})\b",
     "lived_in", "fact", "g1"),
    (rf"\bi(?:'m| am)\s+from\s+({_CAP})\b",
     "from", "fact", "g1"),
    # Pets ------------------------------------------------------------------
    (rf"\bmy\s+(dog|cat|bird|hamster|rabbit|fish|turtle)\s+(?:is\s+)?(?:named\s+|called\s+)?({_CAP})",
     "owns_pet", "fact", "g2"),
    (rf"\b(?:walking|walked|feeding|fed)\s+({_CAP})\b",
     "owns_pet", "fact", "g1"),
    # Dietary / allergy ------------------------------------------------------
    (r"\bi(?:'m| am)\s+(vegetarian|vegan|pescatarian|kosher|halal)\b",
     "dietary_restriction", "fact", "g1"),
    (r"\bi\s+(?:don't|do\s+not)\s+eat\s+([a-z][\w\s]{1,30})\b",
     "dietary_restriction", "fact", "g1"),
    (r"\bi(?:'m| am)\s+(?:seriously\s+|severely\s+)?allergic\s+to\s+([a-z][\w\s]{1,30})\b",
     "allergic_to", "fact", "g1"),
    # "(seriously) allergic to X" — bare form, common in continuations like
    # "I'm vegetarian, and seriously allergic to shellfish".
    (r"\b(?:seriously\s+|severely\s+)?allergic\s+to\s+([a-z][\w\s]{1,30})\b",
     "allergic_to", "fact", "g1"),
    # Preferences -----------------------------------------------------------
    (r"\bi\s+(?:love|adore|really\s+like)\s+([A-Z]?[\w\s]{1,40})\b",
     "likes", "preference", "g1"),
    (r"\bi\s+(?:hate|can't\s+stand|loathe)\s+([A-Z]?[\w\s]{1,40})\b",
     "dislikes", "preference", "g1"),
    (r"\bi\s+(?:prefer)\s+([A-Z]?[\w\s]{1,40})\b",
     "prefers", "preference", "g1"),
)

_CORRECTION = re.compile(r"\b(actually|sorry|correction|i\s+meant)\b", re.IGNORECASE)
_USER_LINE = re.compile(r"^user(?:\([^)]*\))?:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_TRAILING_CONNECTOR = re.compile(
    r"\s+(as|is|was|in|at|on|for|with|the|a|an|to|from|by|of|and|or|but|so|"
    r"before|after|while|during|this|that|these|those|here|there|now|just|"
    r"who|which|what|where|when|why|how)$",
    re.IGNORECASE,
)


def _trim_trailing_connectors(s: str) -> str:
    """Drop dangling preposition/article/connector words at the end of a captured object."""
    prev = None
    while prev != s:
        prev = s
        s = _TRAILING_CONNECTOR.sub("", s).rstrip()
    return s


def _iter_user_lines(messages_text: str) -> Iterable[str]:
    """Yield each user-role line's content. `flatten_messages` prefixes lines."""
    for m in _USER_LINE.finditer(messages_text):
        yield m.group(1).strip()


def _value_for(pred: str, obj: str, raw: str) -> str:
    """Generate a short human-readable summary."""
    obj_clean = obj.strip()
    if pred == "employer":
        return f"Works at {obj_clean}"
    if pred == "lives_in":
        return f"Lives in {obj_clean}"
    if pred == "lived_in":
        return f"Previously lived in {obj_clean}"
    if pred == "from":
        return f"From {obj_clean}"
    if pred == "owns_pet":
        return f"Has a pet named {obj_clean}"
    if pred == "dietary_restriction":
        return f"Dietary: {obj_clean}"
    if pred == "allergic_to":
        return f"Allergic to {obj_clean}"
    if pred == "likes":
        return f"Likes {obj_clean}"
    if pred == "dislikes":
        return f"Dislikes {obj_clean}"
    if pred == "prefers":
        return f"Prefers {obj_clean}"
    return f"{pred}: {obj_clean}"


def _entity_for(pred: str, obj: str) -> list[EntityMention]:
    if pred in ("employer", "previous_employer"):
        return [EntityMention(name=obj, type="org")]
    if pred in ("lives_in", "lived_in", "from"):
        return [EntityMention(name=obj, type="place")]
    if pred == "owns_pet":
        return [EntityMention(name=obj, type="pet")]
    return []


def extract_via_rules(messages_text: str) -> list[MemoryCandidate]:
    """Walk regexes over user lines. Return de-duplicated candidates."""
    out: list[MemoryCandidate] = []
    seen: set[tuple[str, str, str]] = set()  # (subject, predicate, lower_object) dedupe

    for line in _iter_user_lines(messages_text):
        is_correction = bool(_CORRECTION.search(line))
        for pattern, predicate, mtype, target in PATTERNS:
            for m in re.finditer(pattern, line, flags=re.IGNORECASE):
                obj = m.group(2 if target == "g2" else 1)
                if not obj:
                    continue
                obj = obj.strip().rstrip(".,;:!?").strip()
                obj = _trim_trailing_connectors(obj)
                if not obj or len(obj) > 80:
                    continue
                key = ("user", predicate, obj.lower())
                if key in seen:
                    continue
                seen.add(key)
                out.append(MemoryCandidate(
                    type=mtype,                                # type: ignore[arg-type]
                    subject="user",
                    predicate=predicate,
                    object=obj,
                    value=_value_for(predicate, obj, line),
                    raw_quote=line[:240],
                    # Corrections are higher-confidence; everything else is mid-range
                    # because we know rule extraction is brittle.
                    confidence=0.85 if is_correction else 0.6,
                    entities=_entity_for(predicate, obj),
                ))
    return out

"""Text helpers shared across retrievers."""
from __future__ import annotations

import re

# Minimal English stop-word filter for tsquery construction. We're conservative —
# overly aggressive filtering wipes out queries like "what city" that have
# meaningful structure but few content words.
_STOP = {
    # function words
    "the", "a", "an", "is", "are", "was", "were", "be", "to", "of", "in", "on",
    "for", "and", "or", "but", "with", "this", "that", "it", "its", "as", "at",
    "by", "from", "into", "onto", "than", "then", "so", "if", "yet", "not",
    # interrogatives
    "what", "who", "whom", "whose", "where", "when", "why", "how", "which",
    # aux + verbs
    "do", "does", "did", "has", "have", "had", "can", "could", "will", "would",
    "should", "may", "might", "must", "shall", "ought",
    # pronouns
    "i", "me", "my", "mine", "you", "your", "yours", "he", "him", "his", "she",
    "her", "hers", "it", "its", "they", "them", "their", "theirs", "we", "us",
    "our", "ours",
    # conversational fluff
    "any", "some", "tell", "about", "right", "now", "current", "currently",
    "please", "thanks", "thank", "hi", "hey", "hello", "sure", "yes", "no",
    # message-role prefixes injected by flatten_messages — these would match
    # every stored turn and turn FTS into a no-op filter.
    "user", "assistant", "tool", "system",
}

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_-]{1,}", re.UNICODE)


def query_tokens(query: str, *, min_len: int = 2, max_tokens: int = 24) -> list[str]:
    """Tokenize a natural-language query into content words. Lowercased, deduped, stop-filtered."""
    if not query:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _WORD.finditer(query):
        t = m.group(0).lower()
        if len(t) < min_len:
            continue
        if t in _STOP:
            continue
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= max_tokens:
            break
    return out


def to_or_tsquery(query: str) -> str:
    """Build a Postgres tsquery string with OR semantics from a free-text query.

    `plainto_tsquery` joins tokens with AND, which is too strict for natural-language
    questions ("Tell me about Biscuit the dog" → requires 'tell' AND 'biscuit' AND 'dog').
    We hand-build an OR query of stop-filtered content tokens so a turn matches as long
    as it shares any meaningful word with the query. Ranking then orders by overlap.
    """
    toks = query_tokens(query)
    if not toks:
        return ""
    # No prefix matching (`:*`): the English stemmer trims tokens before they
    # hit the index, and `france:*` would then expand to `'franc':*` and
    # ambush "Francisco" (and any other franc-prefix word). Stemming alone
    # gives us plural/conjugation tolerance; we don't need wildcards too.
    return " | ".join(toks)

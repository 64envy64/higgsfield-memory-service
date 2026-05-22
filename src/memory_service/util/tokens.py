"""Token accounting utilities.

We prefer tiktoken (cl100k_base) for accurate counts because the budget logic
in v0.8 will hard-trim against it. When tiktoken can't load (e.g. offline
without cached encodings), we fall back to a chars/4 heuristic — accurate
enough for English to keep the budget meaningful.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_ENC = None


def _enc():
    global _ENC
    if _ENC is not None:
        return _ENC
    try:
        import tiktoken
        _ENC = tiktoken.get_encoding("cl100k_base")
    except Exception as e:
        logger.warning("tiktoken unavailable (%s); using chars/4 heuristic", e)
        _ENC = False
    return _ENC


def approx_token_count(text: str) -> int:
    enc = _enc()
    if enc:
        return len(enc.encode(text or ""))
    return max(1, len(text or "") // 4)


def trim_to_tokens(text: str, max_tokens: int) -> str:
    """Trim text so it encodes to at most max_tokens tokens (or chars*4 heuristic)."""
    if max_tokens <= 0:
        return ""
    enc = _enc()
    if enc:
        tokens = enc.encode(text or "")
        if len(tokens) <= max_tokens:
            return text
        return enc.decode(tokens[:max_tokens])
    char_cap = max_tokens * 4
    return (text or "")[:char_cap]

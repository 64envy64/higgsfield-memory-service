"""Internal extraction types — distinct from API schemas because the contract
between the extractor and the rest of the service is allowed to evolve faster.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class EntityMention:
    name: str
    type: str | None = None  # 'person','pet','place','org','other'


@dataclass
class MemoryCandidate:
    """A single fact/preference/opinion/event extracted from a turn."""
    type: Literal["fact", "preference", "opinion", "event"]
    subject: str                              # "user" or "pet:Biscuit"
    predicate: str                            # canonical or "other:*"
    object: str                               # short canonical object (e.g. "Notion")
    value: str                                # human-readable summary
    raw_quote: str                            # quote from the source message
    confidence: float                         # 0.0..1.0
    entities: list[EntityMention] = field(default_factory=list)

    def key(self) -> str:
        return f"{self.subject}::{self.predicate}"

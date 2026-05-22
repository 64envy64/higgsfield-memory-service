from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class MemoryOut(BaseModel):
    id: str
    type: Literal["fact", "preference", "opinion", "event"]
    subject: str
    predicate: str
    object: str
    key: str
    value: str
    raw_quote: str | None = None
    confidence: float
    source_session: str | None = None
    source_turn: str | None = None
    created_at: datetime
    updated_at: datetime
    supersedes: str | None = None
    active: bool


class MemoriesResponse(BaseModel):
    memories: list[MemoryOut]

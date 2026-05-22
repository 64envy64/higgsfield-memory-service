from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class SearchIn(BaseModel):
    query: str = Field(..., min_length=1, max_length=8_000)
    session_id: str | None = Field(default=None, max_length=256)
    user_id: str | None = Field(default=None, max_length=256)
    limit: int = Field(default=10, ge=1, le=100)


class SearchResult(BaseModel):
    content: str
    score: float
    session_id: str
    timestamp: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchOut(BaseModel):
    results: list[SearchResult]

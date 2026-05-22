from __future__ import annotations

from pydantic import BaseModel, Field


class RecallIn(BaseModel):
    query: str = Field(..., min_length=1, max_length=8_000)
    session_id: str = Field(..., min_length=1, max_length=256)
    user_id: str | None = Field(default=None, max_length=256)
    max_tokens: int = Field(default=1024, ge=16, le=32_000)


class Citation(BaseModel):
    turn_id: str
    score: float
    snippet: str


class RecallOut(BaseModel):
    context: str
    citations: list[Citation]

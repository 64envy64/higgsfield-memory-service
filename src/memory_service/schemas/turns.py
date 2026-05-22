from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class Message(BaseModel):
    role: Literal["user", "assistant", "tool", "system"]
    content: str = Field(..., max_length=64_000)
    name: str | None = None


class TurnIn(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=256)
    user_id: str | None = Field(default=None, max_length=256)
    messages: list[Message] = Field(..., min_length=1, max_length=64)
    timestamp: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("messages")
    @classmethod
    def messages_not_all_empty(cls, v: list[Message]) -> list[Message]:
        if all(not m.content.strip() for m in v):
            raise ValueError("all messages have empty content")
        return v


class TurnOut(BaseModel):
    id: str

"""API 요청 스키마(상세설계 04 §2~4). 응답·이벤트 본문은 backend.domain을 쓴다."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

SCHEMA_VERSION = 1


def _uuid4(v: str) -> str:
    u = UUID(v)
    if u.version != 4:
        raise ValueError("UUID v4 required")
    return str(u)


class ChatRequest(BaseModel):
    schema_version: int
    thread_id: str
    request_id: str
    message: str = Field(min_length=1, max_length=500)

    _ids = field_validator("thread_id", "request_id")(_uuid4)

    @field_validator("message")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("blank message")
        return v

    @property
    def turn_id(self) -> str:
        return f"{self.thread_id}_{self.request_id}"


class FeedbackRequest(BaseModel):
    thread_id: str
    turn_id: str = Field(max_length=100)
    rating: Literal[1, -1]
    comment: str | None = Field(default=None, max_length=200)

    _tid = field_validator("thread_id")(_uuid4)


class TrackRequest(BaseModel):
    thread_id: str
    turn_id: str | None = Field(default=None, max_length=100)
    event: Literal["card_click", "source_click", "chip_click", "new_thread"]
    target: str | None = Field(default=None, max_length=200)
    card_state: Literal["candidate", "cited"] | None = None

    _tid = field_validator("thread_id")(_uuid4)

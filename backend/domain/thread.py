"""대화 상태 계약 — Firestore threads 문서와 TurnState가 공유한다(상세설계 02 §2·§4-2, 04 §2-1)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class Profile(BaseModel):
    """학생이 대화 중 알려 준 조건. 요청 본문으로 받지 않고 classify가 추출해 병합한다."""

    grade: int | None = Field(default=None, ge=1, le=6)
    status: Literal["enrolled", "on_leave", "unknown"] | None = None
    scholarship: Literal["yes", "no", "unknown"] | None = None  # "모름"을 표현하려고 bool이 아님
    dept: str | None = None

    def merged(self, other: Profile) -> Profile:
        """other에서 채워진 값만 덮어쓴다(None은 기존 값 유지)."""
        return self.model_copy(update=other.model_dump(exclude_none=True))

    def filled(self) -> set[str]:
        return {k for k, v in self.model_dump().items() if v is not None}


class LastTurn(BaseModel):
    """직전 1턴 문맥 — 후속 질문 해석용."""

    query_masked: str
    answer_summary: str = Field(max_length=300)  # 서버가 답변 앞부분을 잘라 생성(모델 호출 없음)
    cited_ids: list[str] = Field(default_factory=list)
    topic: str | None = None
    recent_queries: list[str] = Field(
        default_factory=list
    )  # 이전 질문들(예상 질문 흐름·중복 제외용)


class PendingQuestion(BaseModel):
    """되묻기 상태. 되묻기 답이 오면 검색·의도 기준은 original_query_masked로 복원한다."""

    text: str
    original_query_masked: str
    missing_slots: list[str]
    asked_at: datetime


class Correction(BaseModel):
    """classify의 오타·생략 보정 1건(02 §4-2-1). 내용어가 바뀐 보정만 화면에 표시한다."""

    from_: str = Field(alias="from")
    to: str
    confidence: float = Field(ge=0.0, le=1.0)
    kind: Literal["spelling", "particle", "abbrev"]

    model_config = {"populate_by_name": True}


class ThreadDoc(BaseModel):
    """Firestore threads/{thread_id} — 완료 트랜잭션(04 §2-1)에서만 쓴다."""

    profile: Profile = Field(default_factory=Profile)
    pending_question: PendingQuestion | None = None
    clarification_count: int = Field(default=0, ge=0)
    last_turn: LastTurn | None = None
    last_intent: str | None = None
    updated_at: datetime | None = None
    expires_at: datetime | None = None  # TTL 필드 — now + 24h

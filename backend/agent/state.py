"""LangGraph 턴 상태와 LLM 구조화 출력 스키마(상세설계 02 §2·§4-2)."""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

from backend.domain.answer import Answer, Draft, Fallback, Resolution, ReviewFlag, VerifyReport
from backend.domain.evidence import Evidence, EvidenceNeed
from backend.domain.thread import Correction, LastTurn, PendingQuestion, Profile

Intent = Literal["rule", "procedure", "notice", "calendar", "menu", "location", "out_of_scope"]


class ToolCall(TypedDict):
    id: str
    name: str
    args: dict[str, Any]


class TurnState(TypedDict, total=False):
    # 입력
    thread_id: str
    request_id: str
    query: str  # 마스킹된 query_for_model — 원문은 그래프에 넣지 않는다
    deadline: float  # 턴 마감 시각(monotonic 초)
    started: float  # 턴 시작 시각(monotonic 초) — soft 체크포인트 기준
    # Firestore에서 복원
    profile: Profile
    pending_question: PendingQuestion | None
    clarification_count: int
    last_turn: LastTurn | None
    # 판단
    intent: Intent
    topic: str | None
    missing_slots: list[str]
    effective_query: str
    search_query: str
    evidence_needs: list[str]
    normalized_query: str
    corrections: list[Correction]
    correction_confidence: float
    clarification_candidates: list[str]
    # 도구
    messages: Annotated[list, add_messages]
    evidence: list[Evidence]
    tool_calls_count: int
    llm_calls_count: int
    tool_failures: list[str]
    missing_evidence_needs: list[str]
    resolution: Resolution | None
    verify_report: VerifyReport | None
    review_flags: list[ReviewFlag]
    required_calls: list[ToolCall]
    pending_tool_calls: list[ToolCall]
    act_used: bool
    # 출력
    draft: Draft | None
    answer: Answer | None
    fallback: Fallback | None
    ask: dict[str, Any] | None
    outcome: Literal["answer", "fallback", "ask", "error"] | None
    fallback_reason: (
        Literal["out_of_scope", "no_evidence", "verification_failed", "tool_failure", "deadline"]
        | None
    )
    error_code: str | None


class ClassifyOut(BaseModel):
    """classify 1회 호출의 구조화 출력 — 검색어 확장·evidence_needs·오타 보정 통합."""

    intent: Intent
    in_scope: bool
    extracted_profile: Profile = Field(default_factory=Profile)
    needed_slots: list[str] = Field(default_factory=list)
    answers_pending: bool = False
    is_followup: bool = False
    resolved_query: str = ""
    search_query: str = ""
    topic: str | None = None
    evidence_needs: list[EvidenceNeed] = Field(default_factory=list)
    normalized_query: str = ""
    corrections: list[Correction] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    clarification_candidates: list[str] = Field(default_factory=list)


class ActCall(BaseModel):
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class ActOut(BaseModel):
    """보충 act의 구조화 출력 — 서버가 검증·정규화한 뒤 pending_tool_calls에 넣는다."""

    calls: list[ActCall] = Field(default_factory=list, max_length=4)

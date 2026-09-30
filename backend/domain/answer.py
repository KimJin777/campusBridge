"""답변 초안·검증·판정·최종 답변 계약(상세설계 02 §2·§4-6~4-9, 04 §2-4)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class DraftSentence(BaseModel):
    text: str
    cite_ids: list[str] = Field(default_factory=list)
    supporting_quotes: list[str] = Field(
        default_factory=list
    )  # cite_ids와 같은 순서, 근거 본문의 짧은 구간


class Draft(BaseModel):
    """compose의 구조화 출력. dept는 모델이 만들지 않는다(answer 노드에서 서버가 계산)."""

    sentences: list[DraftSentence] = Field(default_factory=list)
    checklist: list[DraftSentence] = Field(default_factory=list)
    next_actions: list[DraftSentence] = Field(default_factory=list)
    follow_ups: list[str] = Field(default_factory=list)  # 이어서 물어볼 예상 질문(검증 대상 아님)


class ReviewFlag(BaseModel):
    """verify·resolve_evidence가 남기는 표시(02 §4-7)."""

    code: str  # 예: polarity_flip, risky_token, source_conflict, adopted_not_cited
    severity: Literal["low", "medium", "high"]
    source_ids: list[str] = Field(default_factory=list)
    sentence_index: int | None = None
    public_action: Literal["suppress_sentence", "internal_only", "conflict_notice"]


class DroppedSentence(BaseModel):
    section: Literal["sentences", "checklist", "next_actions"]
    index: int
    # no_cite, len_mismatch, unknown_id, quote_too_short, quote_not_found,
    # number_mismatch, suppressed
    reason: str


class VerifyReport(BaseModel):
    total: int
    kept: int
    dropped: list[DroppedSentence] = Field(default_factory=list)


class Conflict(BaseModel):
    type: str  # 예: date_mismatch, requirement_mismatch, out_of_period
    source_ids: list[str]
    needs_review: bool = False


class Resolution(BaseModel):
    """resolve_evidence 산출. compose는 이 결과만 사용한다(02 §4-7-1)."""

    adopted: dict[str, list[str]] = Field(default_factory=dict)  # 사실 종류 → 채택 evidence ID
    supporting: dict[str, list[str]] = Field(default_factory=dict)  # 사실 종류 → 보조 ID
    conflicts: list[Conflict] = Field(default_factory=list)
    review_flags: list[ReviewFlag] = Field(default_factory=list)
    template_sentences: list[str] = Field(default_factory=list)  # 서버 템플릿 문장(그대로 붙임)


class Dept(BaseModel):
    """서버가 계산한 담당 부서(04 §2-4). location_text는 장소 표의 검수된 값(지도·좌표 없음)."""

    dept_id: str
    name: str
    phone: str | None = None
    duties: str | None = None
    location_text: str | None = None
    source_url: str | None = None
    snapshot_at: str | None = None


class AnswerNotice(BaseModel):
    code: str  # 예: source_conflict, requirement_mismatch
    text: str  # 서버 템플릿 문장


class PublicSentence(BaseModel):
    """화면으로 내보내는 문장 — supporting_quotes는 빼고 내보낸다."""

    text: str
    cite_ids: list[str] = Field(default_factory=list)

    @classmethod
    def from_draft(cls, s: DraftSentence) -> PublicSentence:
        return cls(text=s.text, cite_ids=list(s.cite_ids))


SAFETY_NOTICE = "최종 내용은 원문에서 확인하세요"


class Answer(BaseModel):
    """SSE answer 이벤트 본문(04 §2-4)."""

    notices: list[AnswerNotice] = Field(default_factory=list)
    sentences: list[PublicSentence] = Field(default_factory=list)
    checklist: list[PublicSentence] = Field(default_factory=list)
    next_actions: list[PublicSentence] = Field(default_factory=list)
    notice: str = SAFETY_NOTICE  # 일반 안전 안내는 서버 템플릿으로만
    dept: Dept | None = None
    cited: list[str] = Field(default_factory=list)
    as_of: str | None = None
    stale_used: bool = False
    follow_ups: list[str] = Field(default_factory=list)  # 화면 '이어서 물어보기' 칩


FallbackReason = Literal[
    "out_of_scope", "no_evidence", "verification_failed", "tool_failure", "deadline"
]
Outcome = Literal["answer", "fallback", "ask", "error"]

FALLBACK_MESSAGE = "확인된 규정·공지에서 답을 찾지 못했습니다."
# 위치를 물었는데 '규정·공지'라고 답하던 문제(교수님 #778)
LOCATION_FALLBACK_MESSAGE = "확인된 캠퍼스 장소·부서 정보와 학교 안내에서 위치를 찾지 못했습니다."


class Fallback(BaseModel):
    reason: FallbackReason
    message: str = FALLBACK_MESSAGE
    dept: Dept | None = None

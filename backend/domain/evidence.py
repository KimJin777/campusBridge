"""근거·도구 결과 계약 — Agent·도구·API·테스트가 함께 import하는 단일 원본.

기준: 상세설계 02 §2(Evidence), 03 상단(ToolResult), 04 §2-4(EvidenceCard).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

EvidenceKind = Literal["article", "guide", "notice", "calendar", "menu", "department", "place"]

# 03 문서 1절 매핑표의 입력값(사실 종류). 모델은 저장소 구현값(rule/guide)을 고르지 않는다.
EvidenceNeed = Literal[
    "eligibility_or_limit",
    "current_deadline",
    "procedure_and_contact",
    "menu",
    "location",
]

ToolErrorCode = Literal[
    "UPSTREAM_TIMEOUT",
    "UPSTREAM_ERROR",
    "PARSE_ERROR",
    "NOT_FOUND",
    "BAD_INPUT",
    "EXCEPTION",  # run_tools가 도구 예외를 감쌀 때(02 §4-5)
]


class Evidence(BaseModel):
    """이번 턴에 도구가 가져온 근거 1건. 인용은 여기 있는 것만 가능하다.

    id 규칙: 조문 article_id / "guide:{page_id}:{section}" / "notice:{board}:{guid}" /
    "cal:{date}:{idx}" / "menu:{date}" / "dept:{dept_id}" / "place:{place_id}"
    """

    id: str
    kind: EvidenceKind
    title: str
    text: str  # 인용 검증 대상 본문
    url: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)  # department, dept_id, revision_date, has_table, as_of, stale 등


class ToolResult(BaseModel):
    """모든 도구의 유일한 반환 형식. 도구는 예외를 던지지 않고 ok=False를 돌려준다."""

    ok: bool
    items: list[Evidence] = Field(default_factory=list)
    error_code: ToolErrorCode | None = None
    message: str | None = None  # 사용자 안내용 짧은 문구(예: "게시된 식단이 없습니다")
    as_of: datetime | None = None  # 데이터 확인 시각
    stale: bool = False  # 마지막 정상 캐시를 썼는가
    age_seconds: int | None = None
    missing_evidence_needs: list[str] = Field(default_factory=list)  # 근거를 못 찾은 사실 종류
    missing_source_kinds: list[str] = Field(default_factory=list)  # 실패·0건인 출처 종류

    @classmethod
    def fail(cls, error_code: ToolErrorCode, message: str | None = None) -> "ToolResult":
        return cls(ok=False, error_code=error_code, message=message)

    @classmethod
    def empty(cls, message: str = "현재 게시된 정보가 없습니다") -> "ToolResult":
        return cls(ok=True, message=message)


SNIPPET_LEN = 120


class EvidenceCard(BaseModel):
    """SSE evidence 이벤트로 내보내는 카드(04 §2-4). 본문 전체·supporting_quotes는 내보내지 않는다."""

    id: str
    kind: EvidenceKind
    state: Literal["candidate", "cited"] = "candidate"
    title: str
    snippet: str
    url: str | None = None
    department: str | None = None
    revision_date: str | None = None
    has_table: bool = False
    as_of: str | None = None
    stale: bool = False

    @classmethod
    def from_evidence(cls, ev: Evidence, state: Literal["candidate", "cited"] = "candidate") -> "EvidenceCard":
        m = ev.meta
        as_of = m.get("as_of")
        return cls(
            id=ev.id,
            kind=ev.kind,
            state=state,
            title=ev.title,
            snippet=ev.text[:SNIPPET_LEN],
            url=ev.url,
            department=m.get("department"),
            revision_date=m.get("revision_date"),
            has_table=bool(m.get("has_table", False)),
            as_of=as_of.isoformat() if isinstance(as_of, datetime) else as_of,
            stale=bool(m.get("stale", False)),
        )

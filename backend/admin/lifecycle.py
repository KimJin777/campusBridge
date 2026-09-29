"""교내 문서 상태 전이 규칙(상세설계 04 §6, 01 §5 documents) — 순수 규칙, I/O 없음.

staging ─publish→ publishing ─(Job)→ published ─archive→ archived
   └─reject→ rejected
(어느 상태든) ─purge→ purging ─(Job)→ purged       ※ purge는 문서 ID 확인 문구까지 받는 2단계

- publish: 미리보기 변환이 끝났고(preview_status=ready) 공개 답변 인용 승인 근거가 있어야 한다.
- archive: 기본 삭제 방식 — 검색에서 즉시 빼고(denylist) 버전은 보존.
- purge: 개인정보 오업로드·법적 요청 전용 — denylist 즉시 등록 후 Job이 원본·추출본·색인을 지운다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from backend.domain import AppError

Action = Literal["publish", "reject", "archive", "purge"]


@dataclass(frozen=True)
class Transition:
    from_statuses: frozenset[str]
    to_status: str
    job_action: str | None  # Job이 이어서 처리할 작업(없으면 상태만 바뀜)
    disable: bool  # 검색 denylist에 즉시 등록


TRANSITIONS: dict[str, Transition] = {
    "publish": Transition(frozenset({"staging"}), "publishing", "publish", False),
    "reject": Transition(frozenset({"staging"}), "rejected", None, False),
    "archive": Transition(frozenset({"published"}), "archived", "archive", True),
    "purge": Transition(
        frozenset({"staging", "rejected", "publishing", "published", "archived"}),
        "purging",
        "purge",
        True,
    ),
}


def check_transition(
    action: str,
    current: dict[str, Any],
    *,
    request_id: str,
    confirm_document_id: str | None = None,
    document_id: str,
) -> tuple[Transition, bool]:
    """(전이 규칙, 이미 같은 요청으로 처리됨 여부). 허용되지 않으면 AppError."""
    rule = TRANSITIONS.get(action)
    if rule is None:
        raise AppError("BAD_REQUEST", "지원하지 않는 문서 작업입니다.")
    status = str(current.get("status") or "")
    if status == rule.to_status and current.get("last_request_id") == request_id:
        return rule, True  # 같은 요청 재전송 — 멱등
    if status not in rule.from_statuses:
        raise AppError(
            "BAD_REQUEST", f"현재 상태({status or '없음'})에서는 이 작업을 할 수 없습니다."
        )
    if action == "publish":
        if current.get("preview_status") != "ready":
            raise AppError("BAD_REQUEST", "미리보기 변환이 끝난 뒤에 게시할 수 있습니다.")
        if not str(current.get("approval_basis") or "").strip():
            raise AppError("BAD_REQUEST", "공개 답변 인용 승인 근거가 없어 게시할 수 없습니다.")
    if action == "purge" and confirm_document_id != document_id:
        raise AppError("BAD_REQUEST", "완전 삭제는 문서 ID를 확인 문구로 다시 입력해야 합니다.")
    return rule, False

"""출처 간 판정(상세설계 02 §4-7-1) — 결정적 규칙, LLM 없음.

사실 종류별 우선 출처로 채택 ID를 정하고, 같은 종류 안의 충돌은 아래 규칙으로만 판정한다.
결과 문구는 서버 템플릿이다(모델이 만들지 않는다).

도구 meta 계약(학사일정·공지 Evidence):
- semester: "2026-2" 형식(없으면 비교하지 않음)
- topic: 절차 키(예: "휴학", "수강신청") — 같은 절차끼리만 날짜를 비교
- start, end: ISO 날짜 문자열(해당 기간), published_at: 공지 게시 시각(ISO)
"""

from __future__ import annotations

import re

from backend.domain.answer import Conflict, Resolution, ReviewFlag
from backend.domain.evidence import Evidence

NEED_PRIORITY: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "eligibility_or_limit": (("article",), ("guide",)),
    "current_deadline": (("calendar", "notice"), ("article",)),
    "procedure_and_contact": (("guide",), ("article",)),
    "menu": (("menu",), ()),
    "location": (("place",), ()),
}

CHANGE_MARK = re.compile(r"변경|연장|정정|수정")

OVERRIDE_TEMPLATE = (
    "학사일정 이후 게시된 공지「{title}」에 변경 내용이 있어 공지 기준으로 안내합니다."
)
MISMATCH_TEMPLATE = (
    "학사일정과 공지「{title}」의 {topic} 기간이 서로 다릅니다. 두 출처를 모두 확인하시고 "
    "정확한 기간은 담당 부서에 문의해 주세요."
)


def _period(e: Evidence) -> tuple[str | None, str | None]:
    return e.meta.get("start"), e.meta.get("end")


def _same_procedure(a: Evidence, b: Evidence) -> bool:
    sa, sb = a.meta.get("semester"), b.meta.get("semester")
    ta, tb = a.meta.get("topic"), b.meta.get("topic")
    return bool(sa and sb and ta and tb and sa == sb and ta == tb)


def _later(notice: Evidence, cal: Evidence) -> bool:
    n, c = notice.meta.get("published_at"), cal.meta.get("published_at")
    if not n:
        return False
    return c is None or str(n) > str(c)


def resolve(evidence: list[Evidence], needs: list[str]) -> Resolution:
    r = Resolution()
    for need in needs:
        primary, secondary = NEED_PRIORITY.get(need, ((), ()))
        adopted = [e.id for e in evidence if e.kind in primary]
        support = [e.id for e in evidence if e.kind in secondary]
        if adopted:
            r.adopted[need] = adopted
        if support:
            r.supporting[need] = support

    if "current_deadline" in needs:
        _deadline_conflicts(evidence, r)
    return r


def _deadline_conflicts(evidence: list[Evidence], r: Resolution) -> None:
    cals = [e for e in evidence if e.kind == "calendar"]
    notices = [e for e in evidence if e.kind == "notice"]
    preferred: list[str] = []
    for n in notices:
        for c in cals:
            if not _same_procedure(n, c) or _period(n) == (None, None):
                continue
            if _period(n) == _period(c):
                continue  # 같은 날짜 — 충돌 아님
            ids = [n.id, c.id]
            text = f"{n.title} {n.text[:200]}"
            if CHANGE_MARK.search(text) and _later(n, c):
                r.conflicts.append(Conflict(type="notice_overrides_calendar", source_ids=ids))
                preferred.append(n.id)
                r.template_sentences.append(OVERRIDE_TEMPLATE.format(title=n.title))
                r.review_flags.append(
                    ReviewFlag(
                        code="notice_overrides_calendar",
                        severity="medium",
                        source_ids=ids,
                        public_action="conflict_notice",
                    )
                )
            else:
                r.conflicts.append(
                    Conflict(type="date_mismatch", source_ids=ids, needs_review=True)
                )
                r.template_sentences.append(
                    MISMATCH_TEMPLATE.format(title=n.title, topic=n.meta.get("topic", ""))
                )
                r.review_flags.append(
                    ReviewFlag(
                        code="date_mismatch",
                        severity="high",
                        source_ids=ids,
                        public_action="conflict_notice",
                    )
                )
    if preferred:
        rest = [i for i in r.adopted.get("current_deadline", []) if i not in preferred]
        r.adopted["current_deadline"] = list(dict.fromkeys(preferred)) + rest
    r.template_sentences = list(dict.fromkeys(r.template_sentences))

"""evidence_needs → 필수 도구 호출 계획(결정적 서버 매핑, 상세설계 03 §1, 02 §4-5-0)."""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

CONTACT_Q = re.compile(r"전화|연락처|번호|팩스|사무실")

# 사실 종류 → 검색 kinds(순서 유지: procedure는 guide 우선)
NEED_KINDS: dict[str, list[str]] = {
    "eligibility_or_limit": ["rule"],
    "current_deadline": ["rule"],
    "procedure_and_contact": ["guide", "rule"],
    # 장소표에 없는 곳(신설 사업단 등)은 학사안내·등록 홈페이지에서 찾는다(교수님 #778)
    "location": ["guide"],
}

BOARD_KEYWORDS = (("scholarship", ("장학",)), ("general", ("일반공지", "행사", "채용")))


def notice_board(topic: str | None, query: str = "") -> str:
    """질문 주제로 공지 게시판을 고른다(결정적). 기본은 학사공지."""
    hay = f"{topic or ''} {query}"
    for board, keys in BOARD_KEYWORDS:
        if any(k in hay for k in keys):
            return board
    return "academic"


def notice_keyword(topic: str | None) -> str:
    """게시판 이름과 겹치는 일반어는 키워드에서 뺀다(예: "장학 공지" → "")."""
    words = [w for w in (topic or "").replace("공지사항", " ").replace("공지", " ").split()]
    # "이번 주 주요 학교 공지" 같은 일반어만 남으면 전체 최신 공지(키워드 없음)
    generic = {
        "장학",
        "학사",
        "일반",
        "주요",
        "학교",
        "이번",
        "주",
        "최근",
        "새",
        "사항",
        "소식",
        "안내",
    }
    words = [w for w in words if w not in generic]
    return " ".join(words)


RELATIVE_DAYS = (("모레", 2), ("내일", 1), ("어제", -1), ("오늘", 0))
WEEK_SHIFT = (("지난주", -7), ("저번주", -7), ("지난 주", -7), ("다음주", 7), ("다음 주", 7))
WEEKDAYS = ("월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일")


def target_day(query: str, today: date) -> date:
    """'내일 학식'·'지난주 목요일 식단' 같은 상대 날짜를 실제 날짜로(#17, 교수님 #629)."""
    for word, delta in RELATIVE_DAYS:
        if word in query:
            return today + timedelta(days=delta)
    base = today
    for word, delta in WEEK_SHIFT:
        if word in query:
            base = today + timedelta(days=delta)
            break
    for i, name in enumerate(WEEKDAYS):
        if name in query or f"{name[0]}요" in query.replace(" ", ""):
            return base - timedelta(days=base.weekday()) + timedelta(days=i)
    return base


def semester_window(today: date) -> tuple[date, date]:
    """이번 학기 전체(1학기 3~8월, 2학기 9월~이듬해 2월).

    지난 일정도 조회하도록(20문항 #1·6·7·15).
    """
    y, m = today.year, today.month
    if 3 <= m <= 8:
        return date(y, 3, 1), date(y, 8, 31)
    start_year = y if m >= 9 else y - 1
    end = date(start_year + 1, 3, 1) - timedelta(days=1)
    return date(start_year, 9, 1), end


def plan_calls(
    evidence_needs: list[str],
    *,
    search_query: str,
    original_query: str | None = None,
    topic: str | None = None,
    today: str | None = None,
    calendar_range: tuple[str, str] | None = None,
    intent: str | None = None,
    notice_days: int | None = None,
) -> list[dict[str, Any]]:
    """필수 호출 목록. 각 원소는 {"id", "name", "args"} — run_tools의 실행 큐 형식."""
    calls: list[dict[str, Any]] = []

    kinds: list[str] = []
    for need in evidence_needs:
        for k in NEED_KINDS.get(need, []):
            if k not in kinds:
                kinds.append(k)
    if "procedure_and_contact" in evidence_needs:
        kinds.sort(key=lambda k: 0 if k == "guide" else 1)
    if kinds:
        args: dict[str, Any] = {"query": search_query, "kinds": kinds}
        if original_query and original_query != search_query:
            args["original_query"] = original_query
        calls.append({"name": "search_academic_knowledge", "args": args})

    if "current_deadline" in evidence_needs:
        cal_args: dict[str, Any] = {}
        if calendar_range:
            cal_args = {"start": calendar_range[0], "end": calendar_range[1]}
        calls.append({"name": "get_academic_calendar", "args": cal_args})
        board = notice_board(topic, search_query)
        notice_args: dict[str, Any] = {"board": board, "keyword": notice_keyword(topic)}
        if notice_days:
            notice_args["days"] = notice_days
        calls.append({"name": "get_notices", "args": notice_args})
    elif intent == "notice":
        board = notice_board(topic, search_query)
        calls.append(
            {"name": "get_notices", "args": {"board": board, "keyword": notice_keyword(topic)}}
        )

    if "menu" in evidence_needs:
        calls.append({"name": "get_menu", "args": {"day": today} if today else {}})

    if "location" in evidence_needs:
        calls.append({"name": "find_campus_location", "args": {"query": search_query}})
        # 승인된 학생 꿀팁은 위치 질문에서만(공식 규정·일정 질문에는 호출하지 않는다 — #715)
        calls.append(
            {"name": "find_campus_tips", "args": {"query": original_query or search_query}}
        )

    # "○○학과 전화번호" → 전화번호부 근거(2026-10-01). 위치 질문이 아니면 꿀팁은 안 부름
    elif "procedure_and_contact" in evidence_needs and CONTACT_Q.search(
        original_query or search_query
    ):
        calls.append({"name": "find_campus_location", "args": {"query": search_query}})

    for i, c in enumerate(calls):
        c["id"] = f"plan_{i}"
    return calls

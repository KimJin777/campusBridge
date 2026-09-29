"""evidence_needs → 필수 도구 호출 계획(결정적 서버 매핑, 상세설계 03 §1, 02 §4-5-0)."""

from __future__ import annotations

from typing import Any

# 사실 종류 → 검색 kinds(순서 유지: procedure는 guide 우선)
NEED_KINDS: dict[str, list[str]] = {
    "eligibility_or_limit": ["rule"],
    "current_deadline": ["rule"],
    "procedure_and_contact": ["guide", "rule"],
}

NOTICE_BOARD_BY_INTENT = {"notice": "academic"}


def plan_calls(
    evidence_needs: list[str],
    *,
    search_query: str,
    original_query: str | None = None,
    topic: str | None = None,
    today: str | None = None,
    calendar_range: tuple[str, str] | None = None,
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
        calls.append({"name": "get_notices", "args": {"board": "academic", "keyword": topic or ""}})

    if "menu" in evidence_needs:
        calls.append({"name": "get_menu", "args": {"day": today} if today else {}})

    if "location" in evidence_needs:
        calls.append({"name": "find_campus_location", "args": {"query": search_query}})

    for i, c in enumerate(calls):
        c["id"] = f"plan_{i}"
    return calls

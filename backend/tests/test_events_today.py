from datetime import date

from backend.app.events import pick_events

T = date(2026, 9, 29)


def row(title, start, end, status="active"):
    return {
        "title": title,
        "start_date": start,
        "end_date": end,
        "status": status,
        "source_url": "https://www.kyungnam.ac.kr/x",
    }


def test_pick_ongoing_and_soon_deadlines_sorted():
    rows = [
        row("학기 운영", "2026-09-01", "2026-12-20"),  # 진행 중(마감 멀다)
        row("장학 신청", "2026-09-29", "2026-10-02"),  # D-3
        row("오늘 마감", None, "2026-09-29"),  # D-day
        row("다음 달 시작", "2026-10-20", "2026-10-25"),  # 아직 시작 전·마감 멀다 → 제외
        row("지난 일정", "2026-09-01", "2026-09-05"),  # 끝남 → 제외
        row("숨김", "2026-09-29", "2026-09-30", "disabled"),  # 숨김 → 제외
        row("검수 대기", "2026-09-29", "2026-09-30", "pending"),
        row("다음 주 시작 마감", "2026-10-01", "2026-10-06"),  # 시작 전이지만 7일 안 마감 → 포함
    ]
    out = pick_events(rows, T)
    assert [(e["title"], e["badge"]) for e in out] == [
        ("오늘 마감", "D-day"),
        ("장학 신청", "D-3"),
        ("다음 주 시작 마감", "D-7"),
        ("학기 운영", "진행 중"),
    ]


def test_pick_week_includes_events_starting_within_seven_days():
    """1주일치: 7일 안에 시작하면 보이고, 그 뒤 시작은 전체보기로(교수님 2026-09-30)."""
    rows = [
        row("엿새 뒤 시작", "2026-10-05", "2026-10-30"),
        row("이레 뒤 시작", "2026-10-06", "2026-10-30"),
    ]
    assert [e["title"] for e in pick_events(rows, T)] == ["엿새 뒤 시작"]


def test_pick_month_overlapping_active_sorted_by_start():
    from backend.app.events import pick_month

    rows = [
        row("걸친 일정", "2026-09-25", "2026-10-03"),
        row("10월 행사", "2026-10-15", "2026-10-15"),
        row("마감만", None, "2026-10-09"),
        row("11월", "2026-11-02", "2026-11-05"),
        row("검수 대기", "2026-10-10", "2026-10-11", "pending"),
    ]
    out = pick_month(rows, date(2026, 10, 1), date(2026, 10, 31))
    assert [(e["title"], e["start"]) for e in out] == [
        ("걸친 일정", "2026-09-25"),
        ("마감만", "2026-10-09"),
        ("10월 행사", "2026-10-15"),
    ]

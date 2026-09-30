import datetime as dt
from datetime import date

from backend.ingest.events import ExtractedEvent, collect_events, extract_period
from backend.ingest.job import run_blocks

REF = date(2026, 9, 29)


def test_extract_ranges_and_deadlines():
    p = extract_period("신청기간: 2026. 9. 29.(화) ~ 10. 6.(화) 17:00", REF)
    assert (p.start, p.end) == (date(2026, 9, 29), date(2026, 10, 6))
    p = extract_period("접수 9/29 ~ 10/2", REF)
    assert (p.start, p.end) == (date(2026, 9, 29), date(2026, 10, 2))
    p = extract_period("서류는 10월 6일(월) 18:00까지 제출", REF)
    assert (p.start, p.end) == (None, date(2026, 10, 6))
    p = extract_period("2026.09.29 ~ 2026.10.06 신청", REF)
    assert p.end == date(2026, 10, 6)


def test_extract_year_rollover_and_rejects():
    nov = date(2026, 11, 20)
    p = extract_period("12. 28. ~ 1. 8. 계절학기 신청", nov)
    assert (p.start, p.end) == (date(2026, 12, 28), date(2027, 1, 8))
    p = extract_period("1월 5일까지 신청", nov)
    assert p.end == date(2027, 1, 5)
    assert extract_period("9. 1. ~ 12. 20. 학기 운영", REF) is None  # 60일 초과 → 자동 게시 금지
    assert extract_period("학점 3.5 이상", REF) is None
    assert extract_period("13. 40. ~ 13. 41.", REF) is None


class Docs:
    def __init__(self):
        self.rows: dict[tuple[str, str], dict] = {}

    def get(self, c, i):
        return self.rows.get((c, i))

    def merge(self, c, i, data):
        self.rows.setdefault((c, i), {}).update(data)

    def find(self, c, field, value):
        return [(i, r) for (cc, i), r in self.rows.items() if cc == c and r.get(field) == value]


NOW = dt.datetime(2026, 9, 29, 3, tzinfo=dt.UTC)


def run(docs, notices, llm=None):
    return collect_events(
        docs,
        today=REF,
        now=NOW,
        fetch_calendar=lambda: [
            {
                "title": "수강신청 정정",
                "start": REF,
                "end": date(2026, 10, 2),
                "source_url": "https://www.kyungnam.ac.kr/ko/4293/subview.do",
            }
        ],
        fetch_notices=lambda board: notices.get(board, []),
        llm_extract=llm,
    )


def events(docs):
    return [v for (c, _), v in docs.rows.items() if c == "campus_events"]


def test_collect_auto_pending_and_keeps_admin_status():
    docs = Docs()
    notices = {
        "scholarship": [
            {
                "title": "국가장학금 2차 신청 안내",
                "summary": "신청기간 9. 29. ~ 10. 6.",
                "url": "https://www.kyungnam.ac.kr/bbs/ko/1407/1/artclView.do",
                "published": REF,
            },
            {
                "title": "교내장학 서류 제출",
                "summary": "제출 기간은 다음 주 금요일 오후 6시",
                "url": "https://www.kyungnam.ac.kr/bbs/ko/1407/2/artclView.do",
                "published": REF,
            },
        ],
        "academic": [
            {
                "title": "지난 일정",
                "summary": "9. 1. ~ 9. 5. 신청",
                "url": "https://www.kyungnam.ac.kr/bbs/ko/1398/3/artclView.do",
                "published": REF,
            },
        ],
    }
    calls = []

    def llm(text):
        calls.append(text)
        return ExtractedEvent(
            title="교내장학 서류 제출", end_date="2026-10-02", is_academic_or_scholarship=True
        )

    stats = run(docs, notices, llm)
    assert {k: stats[k] for k in ("calendar", "notice_auto", "notice_pending", "skipped")} == {
        "calendar": 1,
        "notice_auto": 1,
        "notice_pending": 1,
        "skipped": 1,
    }
    by_title = {e["title"]: e for e in events(docs)}
    assert by_title["국가장학금 2차 신청 안내"]["status"] == "active"
    assert by_title["교내장학 서류 제출"]["status"] == "pending"
    assert by_title["수강신청 정정"]["extracted_by"] == "calendar"

    # 관리자가 끈 일정은 재수집이 되살리지 않고, 같은 공지는 LLM에 다시 묻지 않는다
    for e in events(docs):
        e["status"] = "disabled"
    run(docs, notices, llm)
    assert all(e["status"] == "disabled" for e in events(docs))
    assert len(calls) == 1


def test_run_blocks_only_fresh_active_runs():
    now = NOW
    assert run_blocks({"status": "running", "updated_at": now - dt.timedelta(minutes=5)}, now)
    assert not run_blocks({"status": "running", "updated_at": now - dt.timedelta(hours=3)}, now)
    assert not run_blocks({"status": "success", "updated_at": now}, now)
    assert not run_blocks(None, now)


def test_campus_events_filter_label_and_scan_once():
    from backend.ingest.events import EventCheck

    docs = Docs()
    url = "https://www.kyungnam.ac.kr/bbs/ko/1408/{}/artclView.do"
    notices = {
        "general": [
            {
                "title": "합격자 선배 초청 특강 개최 안내",
                "summary": "",
                "url": url.format(1),
                "published": REF,
            },
            {
                "title": "하나은행 나라사랑카드 홍보",
                "summary": "",
                "url": url.format(2),
                "published": REF,
            },
            {
                "title": "해외봉사 참가학생 모집",
                "summary": "신청 10. 1.(목)까지",
                "url": url.format(3),
                "published": REF,
            },
            {
                "title": "오래된 행사",
                "summary": "",
                "url": url.format(4),
                "published": date(2026, 5, 1),
            },
        ],
    }
    calls = []

    def check(text):
        calls.append(text)
        if "홍보" in text:
            return EventCheck(is_student_event=False, title="홍보", date_label="기간")
        if "특강" in text:
            return EventCheck(
                is_student_event=True,
                title="합격자 선배 초청 특강",
                start_date="2026-10-07",
                end_date="2026-10-07",
                date_label="행사일",
            )
        return EventCheck(is_student_event=True, title="해외봉사 모집", date_label="신청 마감")

    stats = run(docs, notices)  # check_event 없음 → 행사 수집 안 함
    assert stats["event_auto"] == 0 and not calls
    stats = collect_events(
        docs,
        today=REF,
        now=NOW,
        fetch_calendar=lambda: [],
        fetch_notices=lambda b: notices.get(b, []),
        check_event=check,
        fetch_body=lambda u: "",
    )
    assert (stats["event_auto"], stats["event_pending"], stats["event_rejected"]) == (1, 1, 1)
    by = {e["title"]: e for e in events(docs) if e.get("source_category") == "event"}
    assert (
        by["해외봉사 모집"]["status"] == "active"
        and by["해외봉사 모집"]["date_label"] == "신청 마감"
    )
    assert by["합격자 선배 초청 특강"]["status"] == "pending"  # 날짜를 LLM만 줌 → 검수
    assert len(calls) == 3  # 60일 지난 공지는 판정하지 않음
    collect_events(
        docs,
        today=REF,
        now=NOW,
        fetch_calendar=lambda: [],
        fetch_notices=lambda b: notices.get(b, []),
        check_event=check,
    )
    assert len(calls) == 3  # 같은 공지를 다시 묻지 않음


def test_event_scan_retries_transient_llm_failure():
    """LLM 일시 실패(None)는 영구 스킵이 아니라 SCAN_MAX_ATTEMPTS회까지 재판독(GPT5 #667-1)."""
    from backend.ingest.events import SCAN_MAX_ATTEMPTS

    docs = Docs()
    notices = {"general": [{"title": "특강 안내", "summary": "", "url": "u1", "published": REF}]}
    calls = []

    def fail(text):
        calls.append(text)
        return None

    def once():
        return collect_events(
            docs,
            today=REF,
            now=NOW,
            fetch_calendar=lambda: [],
            fetch_notices=lambda b: notices.get(b, []),
            check_event=fail,
        )

    for _ in range(SCAN_MAX_ATTEMPTS + 2):
        once()
    assert len(calls) == SCAN_MAX_ATTEMPTS  # 실패마다 다시 시도, 한도 뒤에는 중단


def test_campus_events_poster_and_date_missing_and_past_kept():
    from backend.ingest.events import EventCheck

    docs = Docs()
    url = "https://www.kyungnam.ac.kr/bbs/ko/1408/{}/artclView.do"
    notices = {
        "general": [
            {"title": "해외봉사 모집", "summary": "", "url": url.format(11), "published": REF},
            {"title": "선배 초청 특강", "summary": "", "url": url.format(12), "published": REF},
            {
                "title": "지난 박람회",
                "summary": "9. 4. ~ 9. 5. 개최",
                "url": url.format(13),
                "published": date(2026, 9, 1),
            },
        ],
    }
    posters = []

    def check_poster(text, images):
        posters.append(text)
        if "해외봉사" in text:
            return EventCheck(
                is_student_event=True,
                title="KU 해외봉사 모집",
                start_date="2026-09-29",
                end_date="2026-10-10",
                date_label="신청 기간",
            )
        return None  # 포스터 판독 실패 → 글자 판정으로

    def check(text):
        return EventCheck(
            is_student_event=True, title=text.split()[0] + " 행사", date_label="행사일"
        )

    stats = collect_events(
        docs,
        today=REF,
        now=NOW,
        fetch_calendar=lambda: [],
        fetch_notices=lambda b: notices.get(b, []),
        check_event=check,
        fetch_body=lambda u: "",
        fetch_images=lambda u: [b"\xff\xd8\xffjpeg"],
        check_poster=check_poster,
    )
    rows = {e["source_url"][-20:]: e for e in events(docs) if e.get("source_category") == "event"}
    by_title = {e["title"]: e for e in rows.values()}
    assert by_title["KU 해외봉사 모집"]["status"] == "pending"  # 포스터(AI) 날짜 → 검수
    assert by_title["KU 해외봉사 모집"]["end_date"] == "2026-10-10"
    assert by_title["선배 행사"]["date_missing"] is True  # 날짜 못 찾음 → 관리자가 입력
    assert by_title["지난 행사"]["end_date"] == "2026-09-05"  # 지난 행사도 기록
    assert stats["event_poster"] == 1 and len(posters) == 3


def test_calendar_correction_supersedes_old_row_but_keeps_admin_and_outage():
    """학사일정 정정: 이전 자동 행은 superseded, 관리자 결정·원천 장애 시는 보존(GPT5 #675)."""
    cal_url = "https://www.kyungnam.ac.kr/ko/4293/subview.do"

    def collect(rows):
        return collect_events(
            docs,
            today=REF,
            now=NOW,
            fetch_calendar=lambda: rows,
            fetch_notices=lambda b: [],
        )

    docs = Docs()
    old = {
        "title": "중간고사",
        "start": date(2026, 10, 20),
        "end": date(2026, 10, 24),
        "source_url": cal_url,
    }
    admin = {
        "title": "축제",
        "start": date(2026, 10, 7),
        "end": date(2026, 10, 8),
        "source_url": cal_url,
    }
    collect([old, admin])
    by = {r["title"]: (k[1], r) for k, r in docs.rows.items() if k[0] == "campus_events"}
    docs.merge("campus_events", by["축제"][0], {"reviewed_by": "admin@x", "status": "active"})

    stats = collect([])  # 원천 장애(0건) → 아무것도 대체하지 않음
    assert stats["superseded"] == 0

    new = {**old, "end": date(2026, 10, 26)}
    stats = collect([new])
    rows = [r for k, r in docs.rows.items() if k[0] == "campus_events"]
    status = sorted((r["title"], r["end_date"], r["status"]) for r in rows)
    assert status == [
        ("중간고사", "2026-10-24", "superseded"),
        ("중간고사", "2026-10-26", "active"),
        ("축제", "2026-10-08", "active"),  # 관리자가 게시한 행은 원천에서 빠져도 보존
    ]
    assert stats["superseded"] == 1


def test_notice_correction_supersedes_same_url_row():
    """같은 공지의 제목·마감이 정정되면 이전 자동 행을 대체(GPT5 #675)."""
    from backend.ingest.events import upsert_event

    docs = Docs()
    base = {
        "source_type": "notice",
        "source_category": "scholarship",
        "source_url": "https://www.kyungnam.ac.kr/bbs/1",
        "extracted_by": "regex",
        "status": "active",
        "start": date(2026, 9, 29),
    }
    first = upsert_event(docs, {**base, "title": "장학 신청", "end": date(2026, 10, 2)}, NOW)
    second = upsert_event(docs, {**base, "title": "장학 신청(연장)", "end": date(2026, 10, 6)}, NOW)
    assert docs.get("campus_events", first)["status"] == "superseded"
    assert docs.get("campus_events", second)["status"] == "active"


def test_admin_reviewed_event_is_not_overwritten_by_recollection():
    """관리자가 게시한 일정은 재수집이 날짜·상태를 바꾸지 않는다(교수님 2026-09-30)."""
    from backend.ingest.events import upsert_event

    docs = Docs()
    ev = {
        "source_type": "notice",
        "source_category": "event",
        "source_url": "https://www.kyungnam.ac.kr/bbs/ko/1408/199826/artclView.do",
        "extracted_by": "llm",
        "status": "pending",
        "title": "합격자 선배 초청 특강",
        "start": date(2026, 9, 8),
        "end": date(2026, 9, 8),
        "date_missing": True,
    }
    eid = upsert_event(docs, ev, NOW)
    docs.merge(
        "campus_events",
        eid,
        {
            "status": "active",
            "reviewed_by": "prof@x",
            "start_date": "2026-09-29",
            "end_date": "2026-09-29",
            "date_missing": False,
        },
    )
    upsert_event(docs, ev, NOW)
    row = docs.get("campus_events", eid)
    assert (row["status"], row["end_date"], row["date_missing"]) == ("active", "2026-09-29", False)

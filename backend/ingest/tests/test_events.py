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
    assert stats == {"calendar": 1, "notice_auto": 1, "notice_pending": 1, "skipped": 1}
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

"""학사 일정 캘린더(campus_events) 수집 — #596 합의(교수님 승인 2026-09-29).

- 공식 학사일정(monthSchdul.do): 그대로 자동 게시(active).
- 학사·장학 공지 RSS: 제목·요약에서 기간/마감을 정규식으로 뽑으면 자동 게시(active),
  정규식이 못 뽑았지만 일정 단서가 있으면 flash-lite 구조화 추출 → 검수 대기(pending).
- 일반공지(취업·행사·입찰 등)는 대상이 아니다(서비스 범위 규칙).
- 관리자가 바꾼 status(disabled/active)는 재수집이 덮어쓰지 않는다.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol

from pydantic import BaseModel, Field

KST = timezone(timedelta(hours=9), "KST")
MAX_SPAN_DAYS = 60
MAX_LLM_PER_RUN = 15
BOARDS = ("academic", "scholarship")

# 날짜 한 개: 2026. 9. 29.(화) / 2026-09-29 / 9/29 / 10월 6일(월)
_DATE = (
    r"(?:(?P<{p}y>20\d{{2}})\s*(?:[./-]|년)\s*)?"
    r"(?P<{p}m>\d{{1,2}})\s*(?:[./-]|월)\s*(?P<{p}d>\d{{1,2}})\s*(?:일|\.)?"
    r"(?:\s*\([월화수목금토일]\))?"
)
_TIME = r"(?:\s*\d{1,2}\s*:\s*\d{2})?"
RANGE_RE = re.compile(
    r"(?<!\d)" + _DATE.format(p="a") + _TIME + r"\s*[~∼～]\s*" + _DATE.format(p="b") + r"(?!\d)"
)
DEADLINE_RE = re.compile(r"(?<!\d)" + _DATE.format(p="a") + _TIME + r"\s*(?:까지|마감)")
HINT_RE = re.compile(r"(기간|마감|까지|접수|신청|제출).{0,20}\d|\d.{0,20}(기간|마감|까지)")


@dataclass(frozen=True)
class Period:
    start: date | None
    end: date


def _year(raw: str | None, month: int, reference: date) -> int:
    if raw:
        return int(raw)
    # 11~12월 공지에 이듬해 1~2월 일정이 나오면 연도를 넘긴다(반대도 마찬가지)
    if month - reference.month <= -6:
        return reference.year + 1
    if month - reference.month >= 6:
        return reference.year - 1
    return reference.year


def _date(m: re.Match[str], p: str, reference: date) -> date | None:
    month, day = int(m[f"{p}m"]), int(m[f"{p}d"])
    try:
        return date(_year(m[f"{p}y"], month, reference), month, day)
    except ValueError:
        return None


def extract_period(text: str, reference: date) -> Period | None:
    """기간(시작~끝) 또는 마감(~까지)을 뽑는다. 검증 실패면 None(자동 게시 금지)."""
    m = RANGE_RE.search(text)
    if m:
        start = _date(m, "a", reference)
        if start is None:
            return None
        end_ref = start if m["by"] is None else reference
        end = _date(m, "b", end_ref)
        if end is not None and end < start and m["by"] is None:
            try:
                end = end.replace(year=end.year + 1)
            except ValueError:
                end = None
        if end is None or not (start <= end <= start + timedelta(days=MAX_SPAN_DAYS)):
            return None
        return Period(start, end)
    m = DEADLINE_RE.search(text)
    if m:
        end = _date(m, "a", reference)
        return Period(None, end) if end else None
    return None


def event_id(source_type: str, source_url: str, title: str, end: date) -> str:
    raw = f"{source_type}|{source_url}|{title}|{end.isoformat()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


class ExtractedEvent(BaseModel):
    """LLM 추출 스키마(정규식 실패분만 — 결과는 검수 대기)."""

    title: str = Field(description="일정 명칭(예: 2학기 국가장학금 2차 신청)")
    start_date: str | None = Field(default=None, description="YYYY-MM-DD, 없으면 null")
    end_date: str | None = Field(default=None, description="YYYY-MM-DD 마감일, 없으면 null")
    is_academic_or_scholarship: bool = Field(description="대학생 학사·장학 일정이면 true")


EXTRACT_PROMPT = (
    "대학 공지에서 학생이 챙겨야 할 신청·제출 기간이나 마감일을 하나만 뽑아라. "
    "본문에 없는 날짜를 지어내지 말고, 날짜가 없으면 end_date를 null로 둬라. "
    "취업·행사·입찰·홍보는 is_academic_or_scholarship=false."
)


class Docs(Protocol):
    def get(self, collection: str, doc_id: str) -> dict[str, Any] | None: ...
    def merge(self, collection: str, doc_id: str, data: dict[str, Any]) -> None: ...


def upsert_event(docs: Docs, event: dict[str, Any], now: datetime) -> str:
    """새 일정은 계산된 status로 만들고, 기존 일정은 status를 건드리지 않고 내용만 갱신한다."""
    eid = event_id(event["source_type"], event["source_url"], event["title"], event["end"])
    row = {
        "title": event["title"][:120],
        "start_date": event["start"].isoformat() if event.get("start") else None,
        "end_date": event["end"].isoformat(),
        "source_type": event["source_type"],
        "source_category": event["source_category"],
        "source_url": event["source_url"],
        "extracted_by": event["extracted_by"],
        "updated_at": now,
    }
    if docs.get("campus_events", eid) is None:
        row |= {"status": event["status"], "created_at": now}
    docs.merge("campus_events", eid, row)
    return eid


def collect_events(
    docs: Docs,
    *,
    today: date,
    now: datetime,
    fetch_calendar: Callable[[], list[dict[str, Any]]],
    fetch_notices: Callable[[str], list[dict[str, Any]]],
    llm_extract: Callable[[str], ExtractedEvent | None] | None = None,
) -> dict[str, int]:
    """일정 수집 본체(네트워크는 주입 — 테스트 가능). 반환: 건수 통계."""
    stats = {"calendar": 0, "notice_auto": 0, "notice_pending": 0, "skipped": 0}
    for row in fetch_calendar():
        upsert_event(
            docs,
            {
                **row,
                "source_type": "calendar",
                "source_category": "calendar",
                "extracted_by": "calendar",
                "status": "active",
            },
            now,
        )
        stats["calendar"] += 1

    llm_left = MAX_LLM_PER_RUN
    for board in BOARDS:
        for n in fetch_notices(board):
            title, text, url = n["title"], f"{n['title']} {n.get('summary', '')}", n["url"]
            published: date = n.get("published") or today
            period = extract_period(text, published)
            base = {
                "title": title,
                "source_type": "notice",
                "source_category": board,
                "source_url": url,
            }
            if period and period.end >= today:
                upsert_event(
                    docs,
                    {
                        **base,
                        "start": period.start or published,
                        "end": period.end,
                        "extracted_by": "regex",
                        "status": "active",
                    },
                    now,
                )
                stats["notice_auto"] += 1
                continue
            if period or not HINT_RE.search(text) or llm_extract is None or llm_left <= 0:
                stats["skipped"] += 1
                continue
            scan_id = hashlib.sha256(url.encode()).hexdigest()[:20]
            if docs.get("event_scans", scan_id) is not None:  # 같은 공지를 매일 다시 묻지 않는다
                stats["skipped"] += 1
                continue
            llm_left -= 1
            got = llm_extract(text[:1000])
            docs.merge("event_scans", scan_id, {"url": url, "scanned_at": now})
            end = _iso(got.end_date) if got else None
            if not got or not got.is_academic_or_scholarship or end is None or end < today:
                stats["skipped"] += 1
                continue
            start = _iso(got.start_date)
            if start and not (start <= end <= start + timedelta(days=MAX_SPAN_DAYS)):
                start = None
            upsert_event(
                docs,
                {
                    **base,
                    "title": got.title or title,
                    "start": start or published,
                    "end": end,
                    "extracted_by": "llm",
                    "status": "pending",
                },
                now,
            )
            stats["notice_pending"] += 1
    return stats


def _iso(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


# ── 운영 구현(네트워크) ─────────────────────────────────────────────────
def live_sources(settings: Any) -> tuple[Callable[[], list[dict]], Callable[[str], list[dict]]]:
    import feedparser
    from bs4 import BeautifulSoup

    from backend.tools import public_sources as ps

    def fetch_calendar() -> list[dict[str, Any]]:
        today = datetime.now(KST).date()
        res = asyncio.run(
            ps.get_academic_calendar(
                today - timedelta(days=7), today + timedelta(days=120), settings=settings
            )
        )
        rows = []
        for ev in res.items if res.ok else []:
            start, end = _iso(ev.meta.get("start")), _iso(ev.meta.get("end"))
            if end:
                rows.append({"title": ev.title, "start": start, "end": end, "source_url": ev.url})
        return rows

    def fetch_notices(board: str) -> list[dict[str, Any]]:
        path = {"academic": settings.rss_academic, "scholarship": settings.rss_scholarship}[board]
        payload = asyncio.run(ps._fetch(ps.urljoin(ps.BASE_URL, path), settings))
        out = []
        for entry in feedparser.parse(payload).entries:
            link = ps.urljoin(ps.BASE_URL, str(entry.get("link") or ""))
            try:
                link = ps.ensure_allowed_url(link, settings.allowed_hosts)
            except ValueError:
                continue
            published = ps._entry_datetime(entry)
            summary = BeautifulSoup(str(entry.get("summary") or ""), "html.parser").get_text(
                " ", strip=True
            )
            out.append(
                {
                    "title": str(entry.get("title") or "").strip() or "공지",
                    "summary": summary,
                    "url": link,
                    "published": published.astimezone(KST).date() if published else None,
                }
            )
        return out

    return fetch_calendar, fetch_notices


def live_llm(settings: Any) -> Callable[[str], ExtractedEvent | None]:
    from backend.agent.llm import GeminiLLM

    llm = GeminiLLM(settings)

    def extract(text: str) -> ExtractedEvent | None:
        try:
            return asyncio.run(
                llm.structured(ExtractedEvent, EXTRACT_PROMPT, text, node="act", deadline=None)
            )
        except Exception:  # noqa: BLE001 — 추출 실패는 건너뛴다(자동 게시 안 함)
            return None

    return extract

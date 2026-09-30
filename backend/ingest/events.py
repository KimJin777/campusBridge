"""학사 일정 캘린더(campus_events) 수집 — #596 합의(교수님 승인 2026-09-29).

- 공식 학사일정(monthSchdul.do): 그대로 자동 게시(active).
- 학사·장학 공지 RSS: 제목·요약에서 기간/마감을 정규식으로 뽑으면 자동 게시(active).
  못 뽑으면 본문까지 읽어 flash-lite가 추출하고, 마감이 남았으면 자동 게시(교수님 2026-09-30:
  "만료되지 않은 공지는 모두 수집·자동 게시, 달력에도 노출"). 관리자는 숨김으로 회수한다.
- 교내 행사(교수님 2026-09-29): 일반공지·행사/세미나 게시판에서 재학생 대상 행사·프로그램만.
  LLM이 대상 여부를 판정하고(광고·채용·외부 모집·기부 제외), 날짜가 잡히면 자동 게시
  (교수님 2026-09-30), 날짜를 못 찾은 행사만 검수 대기. 새 공지만 1회 판정(event_scans).
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
MAX_LLM_PER_RUN = 120  # 첫 재수집(v2 판정)에 게시판 전체를 훑도록 넉넉히
BOARDS = ("academic", "scholarship")
EVENT_BOARDS = ("general", "events")
MAX_EVENT_LLM_PER_RUN = 100
SCAN_MAX_ATTEMPTS = 3  # LLM 판독이 일시 실패(None)하면 다음 수집에서 다시 시도(GPT5 #667-1)
EVENT_LOOKBACK_DAYS = 60
NOTICE_LOOKBACK_DAYS = 120  # 이보다 오래된 학사·장학 공지는 본문을 읽지 않는다
NOTICE_BODY_CHARS = 2500
POSTER_BODY_CHARS = 80  # 본문 글자가 이보다 적으면 포스터 이미지로 판단
MAX_POSTER_IMAGES = 2
MAX_POSTER_BYTES = 4_000_000
DATE_LABELS = ("행사일", "신청 기간", "신청 마감", "기간")

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


def _scan_done(docs: Docs, scan_id: str) -> bool:
    """판독을 끝낸 공지인가. 일시 실패는 SCAN_MAX_ATTEMPTS회까지 다시 판독한다."""
    row = docs.get("event_scans", scan_id)
    if row is None:
        return False
    return row.get("status") != "retry" or int(row.get("attempts", 0)) >= SCAN_MAX_ATTEMPTS


def _mark_scan(docs: Docs, scan_id: str, url: str, now: datetime, ok: bool, **extra: Any) -> None:
    """판독 결과가 있으면 완료, None(예외·타임아웃)이면 재시도 대상으로 남긴다."""
    row: dict[str, Any] = {"url": url, "scanned_at": now, **extra}
    if ok:
        row["status"] = "done"
    else:
        prev = docs.get("event_scans", scan_id) or {}
        row |= {"status": "retry", "attempts": int(prev.get("attempts", 0)) + 1}
    docs.merge("event_scans", scan_id, row)


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


class EventCheck(BaseModel):
    """교내 행사 판정·추출(일반공지·행사 게시판)."""

    is_student_event: bool = Field(
        description=(
            "재학생 대상 교내 행사·프로그램(특강·설명회·교내 모집·공모전·도서관 행사)이면 "
            "true. 은행·기관 홍보, 기부, 교직원 채용, 외부 기관·기업 모집 광고, 행정 안내는 false"
        )
    )
    title: str = Field(description="짧은 행사명(예: 지역인재 7급 합격자 초청 특강)")
    start_date: str | None = Field(default=None, description="YYYY-MM-DD, 없으면 null")
    end_date: str | None = Field(default=None, description="YYYY-MM-DD, 없으면 null")
    date_label: str = Field(description="날짜의 의미: 행사일 | 신청 기간 | 신청 마감 | 기간")


EVENT_PROMPT = (
    "대학 공지 하나를 보고 재학생 대상 교내 행사·프로그램인지 판정하고, "
    "학생이 챙길 날짜를 하나 뽑아라. 본문에 없는 날짜를 지어내지 말고, 없으면 null. "
    "날짜가 신청 마감이면 date_label='신청 마감'."
)


def _label(text: str, raw: str | None) -> str:
    if raw in DATE_LABELS:
        return raw
    if "마감" in text or "까지" in text:
        return "신청 마감"
    return "기간"


class Docs(Protocol):
    def get(self, collection: str, doc_id: str) -> dict[str, Any] | None: ...
    def merge(self, collection: str, doc_id: str, data: dict[str, Any]) -> None: ...
    def find(self, collection: str, field: str, value: Any) -> list[tuple[str, dict[str, Any]]]: ...


CALENDAR_WINDOW_DAYS = 90  # 학사일정 수집 창(오늘~120일) 안쪽만 정정 판정


def _auto_row(row: dict[str, Any]) -> bool:
    """관리자가 손대지 않은 자동 수집 행인가(관리자 게시·숨김 결정은 보존)."""
    return not row.get("reviewed_by") and row.get("status") in ("active", "pending")


def _supersede(docs: Docs, eid: str, now: datetime, reason: str) -> None:
    docs.merge(
        "campus_events",
        eid,
        {"status": "superseded", "superseded_reason": reason, "updated_at": now},
    )


def supersede_same_url(docs: Docs, keep: str, url: str, now: datetime) -> int:
    """공지·행사는 글 하나에 일정 하나 — 같은 글의 정정 전 행을 대체 처리(GPT5 #675)."""
    n = 0
    for eid, row in docs.find("campus_events", "source_url", url):
        if eid != keep and row.get("source_type") != "calendar" and _auto_row(row):
            _supersede(docs, eid, now, "같은 공지의 정정본으로 대체")
            n += 1
    return n


def reconcile_calendar(docs: Docs, seen: set[str], today: date, now: datetime) -> int:
    """학사일정 원천에서 사라진 자동 게시 행(날짜·제목 정정 전 판)을 superseded로(GPT5 #667-2).

    원천 조회가 비면(장애) 호출하지 않는다. 지난 일정과 수집 창 끝부분은 건드리지 않는다.
    """
    horizon = today + timedelta(days=CALENDAR_WINDOW_DAYS)
    n = 0
    for eid, row in docs.find("campus_events", "source_type", "calendar"):
        if eid in seen or not _auto_row(row):
            continue
        try:
            end = date.fromisoformat(row["end_date"])
            start = date.fromisoformat(row.get("start_date") or row["end_date"])
        except (KeyError, TypeError, ValueError):
            continue
        if end >= today and start <= horizon:
            _supersede(docs, eid, now, "학사일정 원천에서 정정·삭제됨")
            n += 1
    return n


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
        "date_label": event.get("date_label"),
        "date_missing": bool(event.get("date_missing")),
        "updated_at": now,
    }
    current = docs.get("campus_events", eid)
    if current is None:
        row |= {"status": event["status"], "created_at": now}
    elif current.get("reviewed_by"):
        # 관리자가 게시·숨김한 일정은 재수집이 내용·날짜·상태를 바꾸지 않는다(교수님 2026-09-30)
        row = {"updated_at": now}
    docs.merge("campus_events", eid, row)
    if event["source_type"] != "calendar":
        supersede_same_url(docs, eid, event["source_url"], now)
    return eid


def collect_events(
    docs: Docs,
    *,
    today: date,
    now: datetime,
    fetch_calendar: Callable[[], list[dict[str, Any]]],
    fetch_notices: Callable[[str], list[dict[str, Any]]],
    llm_extract: Callable[[str], ExtractedEvent | None] | None = None,
    fetch_body: Callable[[str], str] | None = None,
    check_event: Callable[[str], EventCheck | None] | None = None,
    fetch_images: Callable[[str], list[bytes]] | None = None,
    check_poster: Callable[[str, list[bytes]], EventCheck | None] | None = None,
) -> dict[str, int]:
    """일정 수집 본체(네트워크는 주입 — 테스트 가능). 반환: 건수 통계."""
    stats = {
        "calendar": 0,
        "notice_auto": 0,
        "notice_llm": 0,
        "skipped": 0,
        "event_auto": 0,
        "event_pending": 0,
        "event_rejected": 0,
        "superseded": 0,
    }
    seen: set[str] = set()
    for row in fetch_calendar():
        eid = upsert_event(
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
        seen.add(eid)
        stats["calendar"] += 1
    if seen:  # 원천 조회 실패(0건)면 정정 판정을 하지 않는다
        stats["superseded"] += reconcile_calendar(docs, seen, today, now)

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
            if (
                period
                or llm_extract is None
                or llm_left <= 0
                or published < today - timedelta(days=NOTICE_LOOKBACK_DAYS)
            ):
                stats["skipped"] += 1
                continue
            # v2: 본문까지 읽고 자동 게시(교수님 2026-09-30) — 기존 판정 기록과 따로 한 번 더 훑는다
            scan_id = hashlib.sha256(f"notice2|{url}".encode()).hexdigest()[:20]
            if _scan_done(docs, scan_id):  # 같은 공지를 매일 다시 묻지 않는다
                stats["skipped"] += 1
                continue
            body = ""
            if fetch_body is not None:
                try:
                    body = fetch_body(url)[:NOTICE_BODY_CHARS]
                except Exception:  # noqa: BLE001 — 본문 없이 제목·요약으로
                    body = ""
            full = f"{text} {body}"
            if not HINT_RE.search(full):
                _mark_scan(docs, scan_id, url, now, True)
                stats["skipped"] += 1
                continue
            llm_left -= 1
            got = llm_extract(full[:3000])
            _mark_scan(docs, scan_id, url, now, got is not None)
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
                    "status": "active",
                },
                now,
            )
            stats["notice_llm"] += 1

    if check_event is not None:
        _collect_campus_events(
            docs,
            today,
            now,
            fetch_notices,
            fetch_body,
            check_event,
            stats,
            fetch_images=fetch_images,
            check_poster=check_poster,
        )
    return stats


def _collect_campus_events(
    docs: Docs,
    today: date,
    now: datetime,
    fetch_notices: Callable[[str], list[dict[str, Any]]],
    fetch_body: Callable[[str], str] | None,
    check_event: Callable[[str], EventCheck | None],
    stats: dict[str, int],
    *,
    fetch_images: Callable[[str], list[bytes]] | None = None,
    check_poster: Callable[[str, list[bytes]], EventCheck | None] | None = None,
) -> None:
    """교내 행사: 새 공지만 1회 판정 → 대상이면 날짜를 붙여 게시/검수 대기.

    교수님 2026-09-30: 본문이 포스터 이미지뿐이면 AI가 이미지를 읽고(B),
    그래도 날짜를 못 찾은 학생 행사는 '날짜 확인 필요'로 관리자 목록에 올린다(A).
    지난 행사도 기록으로 남긴다(학생 화면은 종료일이 지난 일정을 보이지 않음).
    """
    left = MAX_EVENT_LLM_PER_RUN
    for board in EVENT_BOARDS:
        for n in fetch_notices(board):
            url = n["url"]
            published: date = n.get("published") or today
            if published < today - timedelta(days=EVENT_LOOKBACK_DAYS):
                continue
            # v3: 날짜가 잡히면 자동 게시(교수님 2026-09-30) — 전체를 한 번 다시 훑는다
            scan_id = hashlib.sha256(f"event3|{url}".encode()).hexdigest()[:20]
            if _scan_done(docs, scan_id) or left <= 0:
                continue
            left -= 1
            body = ""
            if fetch_body is not None:
                try:
                    body = fetch_body(url)
                except Exception:  # noqa: BLE001 — 본문 없이 제목·요약으로 판정
                    body = ""
            text = f"{n['title']} {n.get('summary', '')} {body}"[:1500]
            got = None
            if (
                len(body) < POSTER_BODY_CHARS
                and fetch_images is not None
                and check_poster is not None
            ):
                try:
                    images = fetch_images(url)
                except Exception:  # noqa: BLE001
                    images = []
                if images:
                    got = check_poster(text, images)  # 본문이 이미지뿐 → 포스터 판독
                    if got is not None:
                        stats["event_poster"] = stats.get("event_poster", 0) + 1
            if got is None:
                got = check_event(text)
            _mark_scan(docs, scan_id, url, now, got is not None, kind="event")
            if not got or not got.is_student_event:
                stats["event_rejected"] += 1
                continue
            missing = False
            period = extract_period(text, published)
            if period:
                start, end, by, status = period.start or published, period.end, "regex", "active"
            elif _iso(got.end_date) is not None:
                end = _iso(got.end_date)
                start = _iso(got.start_date)
                if start and not (start <= end <= start + timedelta(days=MAX_SPAN_DAYS)):
                    start = None
                start, by, status = start or published, "llm", "active"
            else:
                # 날짜를 못 찾은 학생 행사 → 관리자가 원문을 보고 날짜를 넣어 게시(A)
                start, end, by, status, missing = published, published, "llm", "pending", True
            upsert_event(
                docs,
                {
                    "title": got.title or n["title"],
                    "start": start,
                    "end": end,
                    "source_type": "notice",
                    "source_category": "event",
                    "source_board": board,  # 일반공지·행사세미나 구분(달력 보기 옵션)
                    "source_url": url,
                    "extracted_by": by,
                    "status": status,
                    "date_label": _label(text, got.date_label),
                    "date_missing": missing,
                },
                now,
            )
            stats["event_auto" if status == "active" else "event_pending"] += 1


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
        path = {
            "academic": settings.rss_academic,
            "scholarship": settings.rss_scholarship,
            "general": settings.rss_general,
            "events": settings.rss_events,
        }[board]
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


def live_body(settings: Any) -> Callable[[str], str]:
    """공지 본문 텍스트(.view-con). 포스터 이미지만 있는 공지는 빈 문자열."""
    from bs4 import BeautifulSoup

    from backend.tools import public_sources as ps

    def fetch_body(url: str) -> str:
        html = asyncio.run(ps._fetch(url, settings))
        node = BeautifulSoup(html, "html.parser").select_one(".view-con")
        return node.get_text(" ", strip=True)[:1200] if node else ""

    return fetch_body


def _live_structured(settings: Any, schema: type[BaseModel], prompt: str):
    from backend.agent.llm import GeminiLLM

    llm = GeminiLLM(settings)

    def run(text: str):
        try:
            return asyncio.run(llm.structured(schema, prompt, text, node="act", deadline=None))
        except Exception:  # noqa: BLE001 — 실패는 건너뛴다(자동 게시 안 함)
            return None

    return run


def live_llm(settings: Any) -> Callable[[str], ExtractedEvent | None]:
    return _live_structured(settings, ExtractedEvent, EXTRACT_PROMPT)


def live_event_check(settings: Any) -> Callable[[str], EventCheck | None]:
    return _live_structured(settings, EventCheck, EVENT_PROMPT)


def live_images(settings: Any) -> Callable[[str], list[bytes]]:
    """공지 본문(.view-con)의 이미지(학교 도메인만, 최대 2장·4MB)."""
    from urllib.parse import urljoin

    from bs4 import BeautifulSoup

    from backend.tools import public_sources as ps

    def fetch(url: str) -> list[bytes]:
        html = asyncio.run(ps._fetch(url, settings))
        node = BeautifulSoup(html, "html.parser").select_one(".view-con")
        out: list[bytes] = []
        for img in (node.select("img[src]") if node else [])[:MAX_POSTER_IMAGES]:
            src = urljoin(url, str(img.get("src")))
            try:
                data = asyncio.run(ps._fetch(src, settings))
            except Exception:  # noqa: BLE001 — 허용 밖 도메인·실패 이미지는 건너뜀
                continue
            if 0 < len(data) <= MAX_POSTER_BYTES:
                out.append(data)
        return out

    return fetch


def _mime(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"GIF8":
        return "image/gif"
    return "image/webp" if data[8:12] == b"WEBP" else "image/jpeg"


def live_poster_check(settings: Any) -> Callable[[str, list[bytes]], EventCheck | None]:
    """포스터 이미지를 Gemini가 직접 읽어 행사 여부·날짜를 판정(결과는 검수 대기)."""
    import base64

    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_google_genai import ChatGoogleGenerativeAI

    chat = ChatGoogleGenerativeAI(
        vertexai=True,
        model=settings.gemini_model,
        project=settings.gcp_project_id or None,
        location=settings.gemini_location,
        temperature=0,
        max_output_tokens=1024,
        max_retries=1,
        thinking_level="low",
    ).with_structured_output(EventCheck)

    def run(text: str, images: list[bytes]) -> EventCheck | None:
        parts: list[Any] = [
            {
                "type": "text",
                "text": f"공지 제목·요약: {text[:500]}\n아래는 공지 본문의 포스터 이미지입니다.",
            }
        ]
        for data in images:
            parts.append(
                {
                    "type": "image",
                    "source_type": "base64",
                    "mime_type": _mime(data),
                    "data": base64.b64encode(data).decode(),
                }
            )
        try:
            return asyncio.run(
                asyncio.wait_for(
                    chat.ainvoke([SystemMessage(EVENT_PROMPT), HumanMessage(content=parts)]),
                    timeout=90,
                )
            )
        except Exception:  # noqa: BLE001 — 판독 실패는 글자 판정으로
            return None

    return run

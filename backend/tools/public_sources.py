"""Read-only tools for official notices, calendar events, and weekly menus."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from time import struct_time
from typing import Any, Literal
from urllib.parse import urljoin

import feedparser
import httpx
from bs4 import BeautifulSoup

from backend.app.config import Settings
from backend.domain import Evidence, ToolResult
from backend.tools.common import ensure_allowed_url, truncate, utc_now

BASE_URL = "https://www.kyungnam.ac.kr"
KST = timezone(timedelta(hours=9), "KST")
DATE_PATTERN = re.compile(
    r"(?P<m1>\d{1,2})[./-](?P<d1>\d{1,2})"
    r"(?:\s*~\s*(?P<m2>\d{1,2})[./-](?P<d2>\d{1,2}))?"
)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(5.0),
        follow_redirects=True,
        headers={"User-Agent": "CampusBridge/0.1 (contest; official-source reader)"},
    )


async def _fetch(url: str, settings: Settings, client: httpx.AsyncClient | None = None) -> bytes:
    ensure_allowed_url(url, settings.allowed_hosts)
    if client is None:
        async with _client() as owned:
            response = await owned.get(url)
    else:
        response = await client.get(url)
    response.raise_for_status()
    ensure_allowed_url(str(response.url), settings.allowed_hosts)
    return response.content


async def _post(
    url: str,
    data: dict[str, str],
    settings: Settings,
    client: httpx.AsyncClient | None = None,
) -> bytes:
    ensure_allowed_url(url, settings.allowed_hosts)
    if client is None:
        async with _client() as owned:
            response = await owned.post(url, data=data)
    else:
        response = await client.post(url, data=data)
    response.raise_for_status()
    ensure_allowed_url(str(response.url), settings.allowed_hosts)
    return response.content


def _entry_datetime(entry: Any) -> datetime | None:
    parsed: struct_time | None = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        return datetime(*parsed[:6], tzinfo=UTC).astimezone(KST)
    raw = str(entry.get("published") or entry.get("updated") or "").strip()
    if not raw:
        return None
    try:
        value = parsedate_to_datetime(raw)
        return value.replace(tzinfo=KST) if value.tzinfo is None else value.astimezone(KST)
    except (TypeError, ValueError, OverflowError):
        pass
    match = re.search(
        r"(?P<year>20\d{2})[.\-/](?P<month>\d{1,2})[.\-/](?P<day>\d{1,2})"
        r"(?:\s+(?P<hour>\d{1,2}):(?P<minute>\d{2}))?",
        raw,
    )
    if not match:
        return None
    try:
        return datetime(
            int(match["year"]),
            int(match["month"]),
            int(match["day"]),
            int(match["hour"] or 0),
            int(match["minute"] or 0),
            tzinfo=KST,
        )
    except ValueError:
        return None


def _notice_id(entry: Any) -> str:
    raw = str(entry.get("id") or entry.get("guid") or entry.get("link") or entry.get("title"))
    return re.sub(r"[^0-9A-Za-z가-힣_-]+", "-", raw).strip("-")[-120:] or "unknown"


async def get_notices(
    board: Literal["academic", "scholarship", "general"],
    keyword: str = "",
    days: int = 30,
    *,
    settings: Settings,
    client: httpx.AsyncClient | None = None,
) -> ToolResult:
    paths = {
        "academic": settings.rss_academic,
        "scholarship": settings.rss_scholarship,
        "general": settings.rss_general,
    }
    if board not in paths:
        return ToolResult.fail("BAD_INPUT", "공지 게시판 종류를 확인해 주세요")
    keyword = truncate(keyword, 100)
    days = max(1, min(int(days), 180))
    url = urljoin(BASE_URL, paths[board])
    try:
        payload = await _fetch(url, settings, client)
        feed = await asyncio.to_thread(feedparser.parse, payload)
    except httpx.TimeoutException:
        return ToolResult.fail("UPSTREAM_TIMEOUT", "공지를 불러오는 데 시간이 초과되었습니다")
    except Exception:
        return ToolResult.fail("UPSTREAM_ERROR", "공지를 불러오지 못했습니다")
    if getattr(feed, "bozo", False) and not getattr(feed, "entries", []):
        return ToolResult.fail("PARSE_ERROR", "공지 형식을 읽지 못했습니다")

    now = utc_now().astimezone(KST)
    cutoff = now - timedelta(days=days)
    terms = [term.casefold() for term in keyword.split() if term]
    rows: list[tuple[datetime | None, Evidence]] = []
    for entry in feed.entries:
        title = str(entry.get("title") or "").strip()
        summary = BeautifulSoup(str(entry.get("summary") or ""), "html.parser").get_text(
            " ", strip=True
        )
        searchable = f"{title} {summary}".casefold()
        if terms and not all(term in searchable for term in terms):
            continue
        published = _entry_datetime(entry)
        if published is not None and published < cutoff:
            continue
        link = urljoin(BASE_URL, str(entry.get("link") or ""))
        try:
            link = ensure_allowed_url(link, settings.allowed_hosts)
        except ValueError:
            continue
        published_text = published.isoformat() if published else "게시일 미상"
        rows.append(
            (
                published,
                Evidence(
                    id=f"notice:{board}:{_notice_id(entry)}",
                    kind="notice",
                    title=title or "공지",
                    text="\n".join(part for part in (title, published_text, summary) if part),
                    url=link,
                    meta={"published_at": published.isoformat() if published else None},
                ),
            )
        )
    rows.sort(key=lambda row: row[0] or datetime.min.replace(tzinfo=UTC), reverse=True)
    items = [item for _, item in rows[:5]]
    return ToolResult(ok=True, items=items, as_of=utc_now()) if items else ToolResult.empty()


def _coerce_date(value: date | str | None, fallback: date) -> date:
    if value is None:
        return fallback
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _calendar_rows(payload: bytes, year_hint: int, source_url: str) -> list[Evidence]:
    soup = BeautifulSoup(payload, "html.parser")
    heading = soup.find("h3", string=re.compile(r"20\d{2}년"))
    year_match = re.search(r"20\d{2}", heading.get_text(" ", strip=True) if heading else "")
    year = int(year_match.group()) if year_match else year_hint
    items: list[Evidence] = []
    for index, row in enumerate(soup.select("tr")):
        cells = [cell.get_text(" ", strip=True) for cell in row.select("th,td")]
        if len(cells) < 2:
            continue
        match = DATE_PATTERN.fullmatch(cells[0].strip())
        if not match or not cells[1].strip():
            continue
        start = date(year, int(match["m1"]), int(match["d1"]))
        end_month = int(match["m2"] or match["m1"])
        end_year = year + (1 if end_month < start.month else 0)
        end = date(end_year, end_month, int(match["d2"] or match["d1"]))
        title = cells[1].strip()
        items.append(
            Evidence(
                id=f"cal:{start.isoformat()}:{index}",
                kind="calendar",
                title=title,
                text=f"{title} {start.isoformat()}~{end.isoformat()}",
                url=source_url,
                meta={"start_date": start.isoformat(), "end_date": end.isoformat()},
            )
        )
    return items


def _months_between(start: date, end: date) -> list[tuple[int, int]]:
    current = start.replace(day=1)
    last = end.replace(day=1)
    months: list[tuple[int, int]] = []
    while current <= last:
        months.append((current.year, current.month))
        current = (
            date(current.year + 1, 1, 1)
            if current.month == 12
            else date(current.year, current.month + 1, 1)
        )
    return months


async def get_academic_calendar(
    start: date | str | None = None,
    end: date | str | None = None,
    *,
    settings: Settings,
    client: httpx.AsyncClient | None = None,
) -> ToolResult:
    today = datetime.now(KST).date()
    try:
        start_date = _coerce_date(start, today)
        end_date = _coerce_date(end, start_date + timedelta(days=60))
    except (TypeError, ValueError):
        return ToolResult.fail("BAD_INPUT", "일정 조회 날짜 형식을 확인해 주세요")
    if end_date < start_date:
        return ToolResult.fail("BAD_INPUT", "종료일은 시작일보다 빠를 수 없습니다")
    end_date = min(end_date, start_date + timedelta(days=120))
    url = f"{BASE_URL}/ko/4293/subview.do"
    endpoint = f"{BASE_URL}/schdulmanage/ko/60/monthSchdul.do"
    try:
        months = _months_between(start_date, end_date)
        payloads = await asyncio.gather(
            *(
                _post(
                    endpoint,
                    {"kind": "", "year": str(year), "month": str(month)},
                    settings,
                    client,
                )
                for year, month in months
            )
        )
        batches = await asyncio.gather(
            *(
                asyncio.to_thread(_calendar_rows, payload, year, url)
                for payload, (year, _) in zip(payloads, months, strict=True)
            )
        )
        unique = {item.id: item for batch in batches for item in batch}
        items = list(unique.values())
    except httpx.TimeoutException:
        return ToolResult.fail("UPSTREAM_TIMEOUT", "학사일정을 불러오는 데 시간이 초과되었습니다")
    except Exception:
        return ToolResult.fail("UPSTREAM_ERROR", "학사일정을 불러오지 못했습니다")
    if not items:
        return ToolResult.fail("PARSE_ERROR", "학사일정 형식을 읽지 못했습니다")
    filtered = [
        item
        for item in items
        if date.fromisoformat(item.meta["start_date"]) <= end_date
        and date.fromisoformat(item.meta["end_date"]) >= start_date
    ]
    return ToolResult(ok=True, items=filtered, as_of=utc_now()) if filtered else ToolResult.empty()


def _week_range(title: str, target: date) -> tuple[date, date] | None:
    match = DATE_PATTERN.search(title)
    if not match:
        return None
    start_month, start_day = int(match["m1"]), int(match["d1"])
    end_month = int(match["m2"] or match["m1"])
    end_day = int(match["d2"] or match["d1"])
    candidates: list[tuple[date, date]] = []
    for year in (target.year - 1, target.year, target.year + 1):
        try:
            first = date(year, start_month, start_day)
            last = date(year + (1 if end_month < start_month else 0), end_month, end_day)
            candidates.append((first, last))
        except ValueError:
            continue
    return next((period for period in candidates if period[0] <= target <= period[1]), None)


def _menu_items(payload: bytes, target: date, source_url: str) -> list[Evidence]:
    soup = BeautifulSoup(payload, "html.parser")
    select = soup.select_one('select[name="selArtclSeq"]')
    if select is None:
        return []
    onchange = str(select.get("onchange") or "")
    group_match = re.search(r"jf_curriculum_chg\('[^']+','(?P<group>\d+)'", onchange)
    group = group_match["group"] if group_match else ""
    items: list[Evidence] = []
    for option in select.select("option[value]"):
        title = option.get_text(" ", strip=True)
        period = _week_range(title, target)
        record_id = str(option.get("value") or "").strip()
        if not period or not record_id or not group:
            continue
        pdf_url = urljoin(BASE_URL, f"/curriculum/ko/{group}/{record_id}/fileDownload.do")
        items.append(
            Evidence(
                id=f"menu:{target.isoformat()}:{group}",
                kind="menu",
                title=f"{target.isoformat()} {title}",
                text=f"{title} (메뉴는 첨부 원문에서 확인)",
                url=pdf_url,
                meta={
                    "record_id": record_id,
                    "start_date": period[0].isoformat(),
                    "end_date": period[1].isoformat(),
                },
            )
        )
    return items


async def get_menu(
    day: date | str | None = None,
    *,
    settings: Settings,
    client: httpx.AsyncClient | None = None,
) -> ToolResult:
    try:
        target = _coerce_date(day, datetime.now(KST).date())
    except (TypeError, ValueError):
        return ToolResult.fail("BAD_INPUT", "식단 조회 날짜 형식을 확인해 주세요")
    urls = (f"{BASE_URL}/ko/4454/subview.do", f"{BASE_URL}/ko/8978/subview.do")
    try:
        payloads = await asyncio.gather(*(_fetch(url, settings, client) for url in urls))
        batches = await asyncio.gather(
            *(
                asyncio.to_thread(_menu_items, payload, target, url)
                for payload, url in zip(payloads, urls, strict=True)
            )
        )
    except httpx.TimeoutException:
        return ToolResult.fail("UPSTREAM_TIMEOUT", "식단을 불러오는 데 시간이 초과되었습니다")
    except Exception:
        return ToolResult.fail("UPSTREAM_ERROR", "식단을 불러오지 못했습니다")
    unique: dict[str, Evidence] = {}
    for batch in batches:
        for item in batch:
            unique.setdefault(item.id, item)
    items = list(unique.values())
    if not items:
        return ToolResult.empty("요청한 날짜에 게시된 식단이 없습니다")
    return ToolResult(
        ok=True,
        items=items,
        message="식단은 첨부 원문에서 확인해 주세요",
        as_of=utc_now(),
    )

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from backend.app.config import Settings
from backend.tools import build_tools
from backend.tools.public_sources import _CACHE, get_academic_calendar, get_menu, get_notices


@pytest.fixture(autouse=True)
def clear_tool_cache() -> None:
    _CACHE.clear()


def _client(body: str, content_type: str = "text/html; charset=utf-8") -> httpx.AsyncClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=body.encode(),
            headers={"content-type": content_type},
            request=request,
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_build_tools_exposes_exact_agent_contract() -> None:
    assert set(build_tools(Settings())) == {
        "search_academic_knowledge",
        "get_notices",
        "get_academic_calendar",
        "get_menu",
        "find_campus_location",
    }


@pytest.mark.asyncio
async def test_notices_filters_keyword_and_orders_by_pubdate() -> None:
    rss = """<?xml version="1.0" encoding="utf-8"?>
    <rss version="2.0"><channel>
      <item><title>휴학 신청 안내</title><guid>a</guid>
        <link>https://www.kyungnam.ac.kr/bbs/ko/1398/1/artclView.do</link>
        <pubDate>Mon, 28 Sep 2026 09:00:00 +0900</pubDate><description>기간 공지</description>
      </item>
      <item><title>수강 신청 안내</title><guid>b</guid>
        <link>https://www.kyungnam.ac.kr/bbs/ko/1398/2/artclView.do</link>
        <pubDate>Sun, 27 Sep 2026 09:00:00 +0900</pubDate>
      </item>
    </channel></rss>"""
    async with _client(rss, "application/rss+xml") as client:
        result = await get_notices(
            "academic", "휴학", days=30, settings=Settings(), client=client
        )

    assert result.ok
    assert [item.title for item in result.items] == ["휴학 신청 안내"]


@pytest.mark.asyncio
async def test_notices_returns_last_good_value_as_stale_on_timeout() -> None:
    rss = """<rss version="2.0"><channel><item><title>휴학 안내</title><guid>a</guid>
      <link>https://www.kyungnam.ac.kr/bbs/ko/1398/1/artclView.do</link>
      <pubDate>Mon, 28 Sep 2026 09:00:00 +0900</pubDate></item></channel></rss>"""
    async with _client(rss, "application/rss+xml") as client:
        fresh = await get_notices("academic", settings=Settings(), client=client)
    saved_at = datetime.now(UTC) - timedelta(minutes=31)
    _CACHE["notices:academic::30"] = (saved_at, fresh)

    async def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        stale = await get_notices("academic", settings=Settings(), client=client)

    assert stale.ok and stale.stale
    assert stale.age_seconds is not None and stale.age_seconds >= 31 * 60
    assert stale.as_of == saved_at


@pytest.mark.asyncio
async def test_calendar_parses_rows_and_filters_overlap() -> None:
    html = """<html><h3>2026년 일정관리</h3><table>
      <tr><th>일자</th><th>주요학사내용</th></tr>
      <tr><td>09.24 ~ 09.25</td><td>추석연휴</td></tr>
      <tr><td>09.29</td><td>복학 접수마감</td></tr>
    </table></html>"""
    async with _client(html) as client:
        result = await get_academic_calendar(
            date(2026, 9, 29), date(2026, 9, 30), settings=Settings(), client=client
        )

    assert result.ok
    assert [item.title for item in result.items] == ["복학 접수마감"]
    assert result.items[0].meta["start_date"] == "2026-09-29"


@pytest.mark.asyncio
async def test_menu_returns_reviewable_attachment_link_without_ocr() -> None:
    html = """<html><select name="selArtclSeq"
      onchange="jf_curriculum_chg('ko','107',this.value);">
      <option value="1619">[단체식단] 9/28~ 10/2 주간메뉴</option>
    </select></html>"""
    async with _client(html) as client:
        result = await get_menu(date(2026, 9, 29), settings=Settings(), client=client)

    assert result.ok
    assert len(result.items) == 1
    assert all(item.url and item.url.endswith("/1619/fileDownload.do") for item in result.items)
    assert result.message == "식단은 첨부 원문에서 확인해 주세요"

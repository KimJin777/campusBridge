import asyncio

import httpx
import pytest

from backend.ingest.webpage import (
    BlockedUrl,
    check_public,
    collect_registered,
    extract_page,
    fetch_html,
    normalize_url,
)

PUBLIC = lambda host: ["203.246.64.10"]  # noqa: E731 — 테스트용 공인 IP 해석기


def test_normalize_url_allows_only_school_https():
    assert (
        normalize_url("https://dorm.kyungnam.ac.kr/a?b=1#x") == "https://dorm.kyungnam.ac.kr/a?b=1"
    )
    for bad in (
        "http://www.kyungnam.ac.kr/",
        "https://kyungnam.ac.kr.evil.com/",
        "https://evilkyungnam.ac.kr/",
        "https://user@www.kyungnam.ac.kr/",
        "https://www.kyungnam.ac.kr:8080/",
        "https://169.254.169.254/",
    ):
        with pytest.raises(BlockedUrl):
            normalize_url(bad)


def test_check_public_blocks_private_and_metadata_ips():
    for ip in ("10.0.0.1", "127.0.0.1", "169.254.169.254", "192.168.0.5", "::1"):
        with pytest.raises(BlockedUrl):
            check_public("x.kyungnam.ac.kr", lambda h, ip=ip: [ip])
    check_public("www.kyungnam.ac.kr", PUBLIC)


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


def test_fetch_rechecks_every_redirect_and_html_only():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/go-out":
            return httpx.Response(302, headers={"location": "https://example.com/"})
        if req.url.path == "/go-in":
            return httpx.Response(302, headers={"location": "/page"})
        if req.url.path == "/pdf":
            return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF")
        return httpx.Response(
            200, headers={"content-type": "text/html; charset=utf-8"}, text="<p>ok</p>"
        )

    base = "https://www.kyungnam.ac.kr"
    final, html = asyncio.run(fetch_html(f"{base}/go-in", client=_client(handler), resolver=PUBLIC))
    assert final == f"{base}/page" and "ok" in html
    with pytest.raises(BlockedUrl):  # 교외로 리다이렉트
        asyncio.run(fetch_html(f"{base}/go-out", client=_client(handler), resolver=PUBLIC))
    with pytest.raises(BlockedUrl):  # HTML 아님
        asyncio.run(fetch_html(f"{base}/pdf", client=_client(handler), resolver=PUBLIC))
    with pytest.raises(BlockedUrl):  # 내부망으로 해석되는 호스트
        asyncio.run(
            fetch_html(f"{base}/page", client=_client(handler), resolver=lambda h: ["10.1.1.1"])
        )


def test_extract_generic_page_without_school_cms():
    html = (
        "<html><head><title>컴퓨터공학부</title></head><body><nav>메뉴 메뉴</nav>"
        "<main><h2>학부 안내</h2><p>컴퓨터공학부 사무실은 제1공학관 3층에 있습니다.</p>"
        "<p>전화 055-249-0000</p></main>"
        "<footer>주소</footer></body></html>"
    )
    title, sections = extract_page(html, "https://cs.kyungnam.ac.kr/")
    assert title == "컴퓨터공학부" and len(sections) == 1
    assert "제1공학관" in sections[0]["body"] and "메뉴" not in sections[0]["body"]


class Docs:
    def __init__(self):
        self.rows = {}

    def merge(self, c, i, d):
        self.rows.setdefault((c, i), {}).update(d)

    def find(self, c, field, value):
        return [(i, r) for (cc, i), r in self.rows.items() if cc == c and r.get(field) == value]


class Index:
    def __init__(self):
        self.deleted = []

    def delete(self, ids):
        self.deleted += ids
        return len(ids)


def test_collect_registered_indexes_active_and_removes_stopped():
    docs, index, imported = Docs(), Index(), []
    page = (
        "<html><head><title>생활관</title></head><body><main>"
        + "생활관 입사 안내 문장. " * 10
        + "</main></body></html>"
    )
    docs.merge(
        "web_pages",
        "a",
        {
            "url": "https://dorm.kyungnam.ac.kr/",
            "status": "active",
            "vertex_ids": ["guide-web-a-9"],
        },
    )
    docs.merge(
        "web_pages",
        "b",
        {"url": "https://x.kyungnam.ac.kr/", "status": "stopped", "vertex_ids": ["guide-web-b-1"]},
    )
    docs.merge("web_pages", "c", {"url": "https://bad.kyungnam.ac.kr/", "status": "active"})

    def fetch(url):
        if "bad" in url:
            raise BlockedUrl("본문을 찾지 못했습니다.")
        return url, page

    stats = collect_registered(
        docs,
        index,
        now="t",
        import_docs=lambda d: imported.extend(d) or True,
        fetch=fetch,
        interval=0,
    )
    assert stats == {"pages": 1, "sections": 1, "failed": 1, "removed": 1}
    assert docs.rows[("web_pages", "a")]["vertex_ids"] == ["guide-web-a-1"]
    assert sorted(index.deleted) == ["guide-web-a-9", "guide-web-b-1"]  # 빠진 절 + 중지된 페이지
    assert docs.rows[("web_pages", "c")]["last_status"] == "error"
    assert imported[0]["structData"]["article_id"] == "guide:web-a:1"

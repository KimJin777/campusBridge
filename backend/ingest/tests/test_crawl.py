# ruff: noqa: E501
"""메뉴 순회 후보 찾기(교수님 #909) — 기본 꺼짐, 후보만, 403/429면 멈춤."""

from __future__ import annotations

from backend.ingest import crawl as C
from backend.ingest.webpage import BlockedUrl

HOME = "https://www.kyungnam.ac.kr/ko/index.do"


def page(title: str, *links: tuple[str, str]) -> str:
    anchors = "".join(f'<a href="{h}">{t}</a>' for h, t in links)
    return f"<html><head><title>경남대학교</title></head><body><div id='_contentBuilder'><h2>{title}</h2></div>{anchors}</body></html>"


SITE = {
    HOME: page(
        "홈",
        ("/ko/4444/subview.do", "주차요금안내"),
        ("/ko/9000/subview.do", "총장 인사말"),
        ("/bbs/ko/1398/artclView.do", "공지 글"),
        ("https://evil.example.com/ko/1/subview.do", "외부"),
        ("/sites/ko/download/a.hwp", "첨부"),
    ),
    "https://www.kyungnam.ac.kr/ko/4444/subview.do": page(
        "주차요금안내", ("/ko/4401/subview.do", "휴학")
    ),
    "https://www.kyungnam.ac.kr/ko/9000/subview.do": page("총장 인사말"),
    "https://www.kyungnam.ac.kr/ko/4401/subview.do": page("휴학"),
}


def test_crawl_is_off_by_default(monkeypatch):
    monkeypatch.delenv("WEB_CRAWL_ENABLED", raising=False)
    assert C.crawl_enabled() is False
    monkeypatch.setenv("WEB_CRAWL_ENABLED", "1")
    assert C.crawl_enabled() is True


def test_menu_links_stay_on_school_menu_pages():
    links = [u for u, _ in C.menu_links(SITE[HOME], HOME)]
    assert links == [
        "https://www.kyungnam.ac.kr/ko/4444/subview.do",
        "https://www.kyungnam.ac.kr/ko/9000/subview.do",
    ]  # 게시판 글·외부·첨부는 따라가지 않음


def test_crawl_finds_student_pages_skips_known_and_waits():
    fetched, slept = [], []

    def fetch(url):
        fetched.append(url)
        return url, SITE[url]

    out = C.crawl(
        fetch, known={"https://www.kyungnam.ac.kr/ko/4401/subview.do"}, sleep=slept.append
    )
    urls = [c["url"] for c in out["candidates"]]
    assert urls == [
        "https://www.kyungnam.ac.kr/ko/4444/subview.do"
    ]  # 인사말은 범위 밖, 휴학은 이미 수집 중
    assert "주차" in out["candidates"][0]["matched"]
    assert len(slept) == len(fetched) - 1 and all(s >= 1.0 for s in slept)


def test_crawl_stops_on_block():
    def fetch(url):
        if url != HOME:
            raise BlockedUrl("학교 서버가 요청을 거부했습니다(HTTP 429).")
        return url, SITE[url]

    out = C.crawl(fetch, known=set(), sleep=lambda s: None)
    assert out["stopped"] and out["pages"] == 2 and out["candidates"] == []

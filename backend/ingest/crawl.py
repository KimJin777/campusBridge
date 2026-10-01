# ruff: noqa: E501 — 설명 문장은 줄바꿈하지 않는다
"""학교 홈페이지 메뉴를 링크 따라 돌며 '학생 생활' 안내 페이지 후보를 찾는다(교수님 #909, 2026-10-01).

- **기본 꺼짐.** 환경 변수 WEB_CRAWL_ENABLED=1일 때만 돈다(수집 Job의 source_ids에 "crawl"을 명시해야 실행).
  교수님: "재귀 크롤링 허용한다. 코드만 짜두고 꺼둘 거다. 나중에 필요할 때 재가동해서 쓴다."
- 찾은 페이지는 **색인하지 않는다.** `crawl_candidates`에 후보로만 남기고, 관리자가 [등록]해야
  기존 [홈페이지 등록] 절차로 수집·색인된다.
- 범위: www.kyungnam.ac.kr 의 `/ko/…/subview.do` 메뉴 페이지만. 게시판 글(artclView·/bbs/)·첨부·로그인은 따라가지 않는다.
- 예절: 요청 사이 1초 이상, 깊이·페이지 수 상한, 403/429면 즉시 멈춘다(우회하지 않음). 식별 User-Agent는 fetch_html이 붙인다.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from collections import deque
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

HOST = "www.kyungnam.ac.kr"
START_URLS = ("https://www.kyungnam.ac.kr/ko/index.do",)
MAX_DEPTH = 3
MAX_PAGES = 300
INTERVAL_S = 1.0
MENU_PATH = re.compile(r"^/ko/\d+/subview\.do$")
SKIP_PATH = re.compile(
    r"/bbs/|artclView|download|login|\.(?:hwp|hwpx|pdf|docx?|xlsx?|zip|jpg|png)$", re.I
)
# 학생 생활 범위(서비스 범위 규칙) — 메뉴 이름·페이지 제목에 이 말이 있으면 후보
STUDENT_WORDS = (
    "학사",
    "수강",
    "성적",
    "시험",
    "휴학",
    "복학",
    "졸업",
    "전과",
    "등록금",
    "장학",
    "학생",
    "생활",
    "편의",
    "복지",
    "시설",
    "주차",
    "교통",
    "통학",
    "버스",
    "식당",
    "식단",
    "기숙",
    "생활관",
    "도서관",
    "증명",
    "보건",
    "상담",
    "취업",
    "진로",
    "동아리",
    "학자금",
    "외국인",
    "유학생",
    "캠퍼스",
    "안내",
)


def crawl_enabled() -> bool:
    return os.getenv("WEB_CRAWL_ENABLED", "0").lower() in ("1", "true", "on")


def candidate_id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def menu_links(html: str, base: str) -> list[tuple[str, str]]:
    """같은 도메인의 메뉴 페이지 링크(주소, 링크 글자)."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = str(a["href"]).strip()
        if href.startswith(("javascript:", "#", "mailto:", "tel:")):
            continue
        url = urljoin(base, href).split("#", 1)[0]
        p = urlparse(url)
        if p.scheme != "https" or p.hostname != HOST or SKIP_PATH.search(p.path):
            continue
        if not MENU_PATH.match(p.path) or p.query:
            continue
        if url not in seen:
            seen.add(url)
            out.append((url, " ".join(a.get_text(" ", strip=True).split())[:60]))
    return out


def page_title(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for sel in ("#_contentBuilder h2", "h2.subTitle", "h3.objHeading_h3", "title"):
        n = soup.select_one(sel)
        if n is not None and n.get_text(strip=True):
            return " ".join(n.get_text(" ", strip=True).split())[:80]
    return ""


def is_student_page(*texts: str) -> list[str]:
    joined = " ".join(t for t in texts if t)
    return [w for w in STUDENT_WORDS if w in joined]


def crawl(
    fetch: Callable[[str], tuple[str, str]],
    *,
    known: set[str],
    start_urls: tuple[str, ...] = START_URLS,
    max_depth: int = MAX_DEPTH,
    max_pages: int = MAX_PAGES,
    interval: float = INTERVAL_S,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """너비 우선 순회. 반환: {candidates: [...], pages, stopped}. 색인·저장은 하지 않는다."""
    from backend.ingest.webpage import BlockedUrl

    queue: deque[tuple[str, int, str]] = deque((u, 0, "") for u in start_urls)
    visited: set[str] = set()
    candidates: list[dict[str, Any]] = []
    stopped: str | None = None
    while queue and len(visited) < max_pages:
        url, depth, link_text = queue.popleft()
        if url in visited:
            continue
        visited.add(url)
        if len(visited) > 1:
            sleep(interval)
        try:
            final, html = fetch(url)
        except BlockedUrl as exc:  # 403·429 등 — 우회하지 않고 멈춘다
            stopped = str(exc)
            break
        except Exception:  # noqa: BLE001 — 한 페이지 실패는 건너뛴다
            continue
        title = page_title(html)
        words = is_student_page(link_text, title)
        if depth > 0 and words and final not in known:
            candidates.append(
                {
                    "url": final,
                    "title": title,
                    "link_text": link_text,
                    "depth": depth,
                    "matched": words[:5],
                }
            )
        if depth < max_depth:
            for nxt, text in menu_links(html, final):
                if nxt not in visited:
                    queue.append((nxt, depth + 1, text))
    return {"candidates": candidates, "pages": len(visited), "stopped": stopped}


def save_candidates(docs: Any, found: dict[str, Any], now: Any) -> int:
    """후보를 crawl_candidates에(이미 있으면 갱신만, 관리자 처리 상태는 유지)."""
    for c in found["candidates"]:
        docs.merge("crawl_candidates", candidate_id(c["url"]), {**c, "seen_at": now})
    return len(found["candidates"])

"""관리자가 등록한 교내 홈페이지 수집(교수님 #682·#685·#688).

- 관리자가 URL을 넣고 미리보기로 확인한 뒤 [등록]하면 바로 수집 대상이 된다(별도 승인 없음).
- 대상은 https://*.kyungnam.ac.kr 교내 홈페이지 한 쪽뿐 — 링크를 따라가지 않는다(재귀 크롤링 없음).
- 서버가 관리자 입력 URL을 가져오므로 SSRF 방어(Gemini #684): 호스트 허용 목록, 공인 IP만,
  리다이렉트마다 재검사, HTML만, 5MB 이하.
- 색인은 학사안내(guide)와 같은 형식: article_id = guide:web-{page_id}:{n}
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import ipaddress
import socket
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

from backend.ingest.guides import (
    MIN_SECTION_CHARS,
    _clean_text,
    _user_agent,
    build_guide_documents,
    extract_sections,
    tables_to_rows,
)

SCHOOL_DOMAIN = "kyungnam.ac.kr"
MAX_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 3
MAX_SECTIONS = 30
CHUNK_CHARS = 1200
DROP_TAGS = ("script", "style", "noscript", "iframe", "header", "footer", "nav", "aside", "form")
ROOT_SELECTORS = ("main", "article", "#content", "#contents", ".content", ".contents", "body")


class BlockedUrl(ValueError):
    """교내 홈페이지가 아니거나 안전하지 않은 주소."""


def normalize_url(url: str) -> str:
    """https + *.kyungnam.ac.kr + 기본 포트만. 조각(#)은 떼고 돌려준다."""
    p = urlparse((url or "").strip())
    host = (p.hostname or "").lower()
    if p.scheme != "https":
        raise BlockedUrl("https 주소만 등록할 수 있습니다.")
    from backend.tools.common import AFFILIATED_DOMAINS, is_school_host

    if not is_school_host(host):
        allowed = ", ".join((SCHOOL_DOMAIN, *AFFILIATED_DOMAINS))
        raise BlockedUrl(f"경남대학교 교내·소속 기관 홈페이지({allowed})만 등록할 수 있습니다.")
    if p.username or p.password or p.port not in (None, 443):
        raise BlockedUrl("주소 형식이 올바르지 않습니다.")
    return urlunparse(("https", host, p.path or "/", "", p.query, ""))


def page_id(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode()).hexdigest()[:12]


Resolver = Callable[[str], list[str]]


def _resolve(host: str) -> list[str]:
    return [str(info[4][0]) for info in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)]


def check_public(host: str, resolver: Resolver = _resolve) -> None:
    """사설망·루프백·메타데이터(169.254.169.254) 등 공인 IP가 아니면 거부."""
    try:
        addrs = resolver(host)
    except OSError as exc:
        raise BlockedUrl("주소를 찾을 수 없습니다.") from exc
    if not addrs or any(not ipaddress.ip_address(a).is_global for a in addrs):
        raise BlockedUrl("내부망 주소로 연결되는 주소는 등록할 수 없습니다.")


async def fetch_html(
    url: str,
    *,
    client: httpx.AsyncClient | None = None,
    resolver: Resolver = _resolve,
) -> tuple[str, str]:
    """(최종 URL, HTML). 리다이렉트는 직접 따라가며 매번 주소·IP를 다시 검사한다."""
    own = client is None
    client = client or httpx.AsyncClient(
        headers={"User-Agent": _user_agent()}, timeout=20, follow_redirects=False
    )
    try:
        current = normalize_url(url)
        for _ in range(MAX_REDIRECTS + 1):
            check_public(urlparse(current).hostname or "", resolver)
            async with client.stream("GET", current) as r:
                if r.is_redirect:
                    current = normalize_url(urljoin(current, r.headers.get("location", "")))
                    continue
                if r.status_code in (403, 429):
                    raise BlockedUrl(f"학교 서버가 요청을 거부했습니다(HTTP {r.status_code}).")
                r.raise_for_status()
                if "text/html" not in r.headers.get("content-type", ""):
                    raise BlockedUrl("HTML 웹 페이지만 등록할 수 있습니다.")
                body = bytearray()
                async for chunk in r.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        raise BlockedUrl("페이지가 너무 큽니다(5MB 초과).")
                return current, body.decode(r.encoding or "utf-8", errors="replace")
        raise BlockedUrl("리다이렉트가 너무 많습니다.")
    finally:
        if own:
            await client.aclose()


def extract_page(html: str, url: str) -> tuple[str, list[dict[str, object]]]:
    """본문 절 목록. 학교 표준 CMS(#_contentBuilder)면 학사안내 추출기를, 아니면 일반 본문 추출."""
    host = urlparse(url).hostname or ""
    hosts = {host, "www.kyungnam.ac.kr"}
    title, sections = extract_sections(html, page_id="web", page_url=url, allowed_hosts=hosts)
    if sections:
        return title, sections[:MAX_SECTIONS]
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else url
    for tag in DROP_TAGS:
        for n in soup.find_all(tag):
            n.decompose()
    root = next((r for sel in ROOT_SELECTORS if (r := soup.select_one(sel)) is not None), None)
    if root is not None:
        tables_to_rows(root)
    text = _clean_text(root) if root is not None else ""
    if len(text) < MIN_SECTION_CHARS:
        return title, []
    chunks: list[str] = []
    buf = ""
    for line in text.splitlines():
        if buf and len(buf) + len(line) > CHUNK_CHARS:
            chunks.append(buf)
            buf = ""
        buf = f"{buf}\n{line}" if buf else line
    if buf:
        chunks.append(buf)
    sections: list[dict[str, object]] = []
    for n, body in enumerate(chunks[:MAX_SECTIONS], start=1):
        heading = title if len(chunks) == 1 else f"{title} ({n})"
        sections.append({"n": n, "heading": heading, "body": f"{heading}\n{body}", "links": []})
    return title, sections


def build_documents(pid: str, url: str, title: str, sections: list[dict[str, object]]) -> list:
    fetched = dt.datetime.now(dt.UTC).isoformat()
    return build_guide_documents(f"web-{pid}", url, title, sections, fetched)


# ── 수집 Job 단계 ──────────────────────────────────────────────────────
class Docs(Protocol):
    def merge(self, collection: str, doc_id: str, data: dict[str, Any]) -> None: ...
    def find(self, collection: str, field: str, value: Any) -> list[tuple[str, dict[str, Any]]]: ...


class Index(Protocol):
    def delete(self, vertex_ids: list[str]) -> int: ...


def collect_registered(
    docs: Docs,
    index: Index,
    *,
    now: Any,
    import_docs: Callable[[list[dict[str, object]]], bool],
    fetch: Callable[[str], tuple[str, str]] | None = None,
    interval: float = 1.0,
) -> dict[str, int]:
    """등록된(active) 페이지를 다시 읽어 색인하고, 중지·삭제된 페이지는 색인에서 뺀다.

    import_docs(documents) → 성공 여부. 성공했을 때만 vertex_ids를 갱신하고 빠진 절을 지운다.
    """
    import time

    fetch = fetch or (lambda u: asyncio.run(fetch_html(u)))
    stats = {"pages": 0, "sections": 0, "failed": 0, "removed": 0}
    plans: list[tuple[str, dict[str, Any], dict[str, Any], list[str]]] = []
    documents: list[dict[str, object]] = []
    active = docs.find("web_pages", "status", "active")
    for i, (pid, row) in enumerate(active):
        try:
            final, html = fetch(row["url"])
            title, sections = extract_page(html, final)
            page_docs = build_documents(pid, row["url"], title, sections)
            if not page_docs:
                raise BlockedUrl("본문을 찾지 못했습니다.")
            documents += page_docs
            ids = [str(d["id"]) for d in page_docs]
            update = {"title": title, "sections": len(ids), "last_status": "ok", "last_error": None}
            plans.append((pid, row, update, ids))
            stats["pages"] += 1
            stats["sections"] += len(ids)
        except Exception as exc:  # noqa: BLE001 — 페이지 단위 격리(기존 색인은 유지)
            stats["failed"] += 1
            reason = str(exc) if isinstance(exc, BlockedUrl) else type(exc).__name__
            docs.merge(
                "web_pages", pid, {"last_run_at": now, "last_status": "error", "last_error": reason}
            )
        if interval and i < len(active) - 1:
            time.sleep(interval)
    ok = import_docs(documents) if documents else True
    for pid, row, update, ids in plans:
        if not ok:
            docs.merge(
                "web_pages",
                pid,
                {"last_run_at": now, "last_status": "error", "last_error": "색인 반영 실패"},
            )
            continue
        stale = sorted(set(row.get("vertex_ids") or []) - set(ids))
        if stale:
            index.delete(stale)
        docs.merge("web_pages", pid, {**update, "last_run_at": now, "vertex_ids": ids})
    for status in ("stopped", "deleted"):
        for pid, row in docs.find("web_pages", "status", status):
            ids = list(row.get("vertex_ids") or [])
            if ids:
                index.delete(ids)
                docs.merge("web_pages", pid, {"vertex_ids": [], "removed_at": now})
                stats["removed"] += 1
    return stats

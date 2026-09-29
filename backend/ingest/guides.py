"""학사안내(guide) 페이지 수집·색인(상세설계 01 §0-2, 03 §1, D15).

실행:
    uv run python -m backend.ingest.guides --import-index

- 대상은 config/sources.yaml의 academic_guides만(허용 목록, 재귀 크롤링 없음), 요청 간격 1초·식별 UA
- 본문 선택자는 D15 확인값: `#_contentBuilder`, 절 제목은 `h3.objHeading_h3`
- 절(section) 단위 문서: id = guide:{page_id}:{n}, 제목 "{페이지명} — {절 제목}"
- 행동 링크(신청·서식 등)는 허용 호스트의 https 링크만 meta.links로 보존
- 정제 결과가 너무 짧으면(MIN_SECTION_CHARS 미만) 그 절은 색인하지 않는다
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import re
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
import yaml
from bs4 import BeautifulSoup, Tag

from backend.ingest.indexing import _write_jsonl

BASE_URL = "https://www.kyungnam.ac.kr"
CONTENT_SELECTOR = "#_contentBuilder"
SECTION_SELECTOR = "h3.objHeading_h3"
DROP_SELECTORS = ("script", "style", "h2.hidden", "noscript", "iframe")
EXTRACTOR_VERSION = "guide-v1"
MIN_SECTION_CHARS = 30
DEPT_RE = re.compile(r"([가-힣]{2,12}(?:팀|처|센터))(?:\s*\(|\s|$|[,.)])")
PHONE_RE = re.compile(r"0\d{1,2}-\d{3,4}-\d{4}")


def _user_agent() -> str:
    contact = os.getenv("COLLECTION_CONTACT_EMAIL")
    if not contact:
        raise RuntimeError("COLLECTION_CONTACT_EMAIL is required for identifiable collection")
    return f"CampusBridge/0.1 (contest; contact: {contact})"


def load_pages(config_path: Path) -> tuple[list[dict[str, str]], set[str]]:
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    guides = cfg["sources"]["academic_guides"] if "sources" in cfg else cfg["academic_guides"]
    return list(guides["pages"]), set(guides["allowed_hosts"])


def _clean_text(node: Tag) -> str:
    lines = [ln.strip() for ln in node.get_text("\n").splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _links(anchors: Sequence[Tag], page_url: str, allowed_hosts: set[str]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for a in anchors:
        url = urljoin(page_url, a["href"])
        p = urlparse(url)
        text = a.get_text(" ", strip=True)
        if p.scheme == "https" and p.hostname in allowed_hosts and text:
            out.append({"text": text[:60], "url": url})
    return out[:10]


def extract_sections(
    html: str, *, page_id: str, page_url: str, allowed_hosts: set[str]
) -> tuple[str, list[dict[str, object]]]:
    """페이지 제목과 절 목록을 돌려준다. 절: {n, heading, body, links}."""
    soup = BeautifulSoup(html, "html.parser")
    page_title = soup.title.get_text(strip=True) if soup.title else page_id
    root = soup.select_one(CONTENT_SELECTOR)
    if root is None:
        return page_title, []
    for sel in DROP_SELECTORS:
        for n in root.select(sel):
            n.decompose()
    headings = root.select(SECTION_SELECTOR)
    if not headings:
        body = _clean_text(root)
        if len(body) >= MIN_SECTION_CHARS:
            return page_title, [{"n": 1, "heading": page_title, "body": body, "links": []}]
        return page_title, []
    head_ids = {id(h): i for i, h in enumerate(headings, start=1)}
    buckets: dict[int, list[str]] = {i: [] for i in head_ids.values()}
    current: int | None = None
    for text in root.find_all(string=True):
        t = text.strip()
        if not t:
            continue
        owner = next((head_ids[id(p)] for p in text.parents if id(p) in head_ids), None)
        if owner is not None:
            current = owner  # 절 제목 자체는 본문에 넣지 않음(아래에서 앞에 붙임)
            continue
        if current is not None:
            buckets[current].append(t)
    links: dict[int, list[Tag]] = {i: [] for i in head_ids.values()}
    for a in root.find_all("a", href=True):
        prev = a.find_previous("h3", class_="objHeading_h3")
        if prev is not None and id(prev) in head_ids:
            links[head_ids[id(prev)]].append(a)
    sections: list[dict[str, object]] = []
    for h in headings:
        n = head_ids[id(h)]
        body = "\n".join(buckets[n])
        if len(body) < MIN_SECTION_CHARS:
            continue
        heading = h.get_text(" ", strip=True)
        sections.append(
            {
                "n": n,
                "heading": heading,
                "body": f"{heading}\n{body}",
                "links": _links(links[n], page_url, allowed_hosts),
            }
        )
    return page_title, sections


def build_guide_documents(
    page_id: str, page_url: str, page_title: str, sections: list[dict[str, object]], fetched_at: str
) -> list[dict[str, object]]:
    docs = []
    all_text = "\n".join(str(s["body"]) for s in sections)
    page_dept = (DEPT_RE.search(all_text) or [None, None])[1]
    for s in sections:
        body = str(s["body"])
        dept_match = DEPT_RE.search(body)
        phone = PHONE_RE.search(body)
        data: dict[str, object] = {
            "article_id": f"guide:{page_id}:{s['n']}",
            "article_title": f"{page_title} — {s['heading']}",
            "rule_name": page_title,
            "section": str(s["heading"]),
            "kind": "guide",
            "source_kind": "guide",
            "access": "public",
            "source_url": page_url,
            "body": body,
            "department": dept_match.group(1) if dept_match else page_dept,
            "phone": phone.group(0) if phone else None,
            "fetched_at": fetched_at,
            "links": json.dumps(s["links"], ensure_ascii=False) if s["links"] else None,
            "extractor_version": EXTRACTOR_VERSION,
        }
        docs.append(
            {
                "id": f"guide-{page_id}-{s['n']}",  # Vertex ID 규칙; 근거 ID는 article_id
                "structData": {k: v for k, v in data.items() if v is not None},
            }
        )
    return docs


async def collect(
    pages: list[dict[str, str]], allowed_hosts: set[str], *, interval: float = 1.0
) -> tuple[list[dict[str, object]], dict[str, object]]:
    docs: list[dict[str, object]] = []
    report: dict[str, object] = {}
    async with httpx.AsyncClient(
        headers={"User-Agent": _user_agent()}, timeout=20, follow_redirects=True
    ) as client:
        for i, page in enumerate(pages):
            url = urljoin(BASE_URL, page["path"])
            try:
                r = await client.get(url)
                final = urlparse(str(r.url))
                if final.scheme != "https" or final.hostname not in allowed_hosts:
                    raise ValueError("redirected outside allowlist")
                if r.status_code in (403, 429):
                    raise RuntimeError(f"collection stopped: HTTP {r.status_code}")
                r.raise_for_status()
                fetched = dt.datetime.now(dt.UTC).isoformat()
                title, sections = extract_sections(
                    r.text, page_id=page["id"], page_url=url, allowed_hosts=allowed_hosts
                )
                page_docs = build_guide_documents(page["id"], url, title, sections, fetched)
                docs += page_docs
                report[page["id"]] = {
                    "status": "ok" if page_docs else "rejected_empty",
                    "title": title,
                    "sections": len(page_docs),
                }
            except RuntimeError:
                raise
            except Exception as exc:  # noqa: BLE001 — 페이지 단위 격리
                report[page["id"]] = {"status": "error", "error": type(exc).__name__}
            if i < len(pages) - 1:
                await asyncio.sleep(interval)
    return docs, report


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Collect and index academic guide pages")
    p.add_argument("--config", type=Path, default=Path("config/sources.yaml"))
    p.add_argument("--output", type=Path, default=Path("data/processed/guides.jsonl"))
    p.add_argument("--report", type=Path, default=Path("data/processed/guides_report.json"))
    p.add_argument("--import-index", action="store_true")
    p.add_argument("--project", default=os.environ.get("GCP_PROJECT_ID", ""))
    p.add_argument("--bucket", default=os.environ.get("RULES_BUCKET", ""))
    p.add_argument(
        "--data-store-id", default=os.environ.get("SEARCH_DATASTORE_ID", "rules-articles")
    )
    p.add_argument(
        "--engine-id", default=os.environ.get("SEARCH_ENGINE_ID", "rules-articles-search")
    )
    args = p.parse_args(argv)
    pages, hosts = load_pages(args.config)
    docs, report = asyncio.run(collect(pages, hosts))
    _write_jsonl(args.output, docs)
    out: dict[str, object] = {"pages": len(pages), "documents": len(docs), "per_page": report}
    if args.import_index:
        from backend.ingest.full_index import import_index

        out["import"] = import_index(
            project=args.project,
            bucket=args.bucket,
            data_store_id=args.data_store_id,
            engine_id=args.engine_id,
            source=args.output,
            object_prefix="guides",
        )
    args.report.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"pages": len(pages), "documents": len(docs)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

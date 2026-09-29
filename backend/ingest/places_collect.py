"""장소·부서 연락처 후보 수집 에이전트(교수님 2026-09-29: 하위 사이트 포함 + 사람 검수 유지).

    uv run python -m backend.ingest.places_collect [--write]

수집원(재귀 없음)
1. 조직도 `/ko/4251` — 조직 이름·링크
2. 캠퍼스투어 `/ko/8013` — 건물 이름(위치 문구 인식용 목록 겸 building 후보)
3. 조직도 링크 페이지 — 팀별 "위치 : 본관 1층 / 전화 : (055)249-2027" 블록
4. 하위 사이트(`*.kyungnam.ac.kr`) 홈 → 같은 호스트의 "찾아오시는 길·위치·연락처" 링크 1단계

규칙
- 위치 문구는 **원문에 글자 그대로** 있어야 하며 원문 문장(evidence_text)·URL·확인일을 함께 저장
- 직원 개인 전화표(이름이 ○○로 가려진 행)는 수집하지 않는다 — 팀 대표 번호만
- 결과는 Firestore `places`에 **pending**으로만 쓴다. verified 행은 덮어쓰지 않는다
- 요청 간격 1초·식별 UA·https + kyungnam.ac.kr만·403/429 즉시 중단
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import os
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

BASE = "https://www.kyungnam.ac.kr"
ORG_URL = f"{BASE}/ko/4251/subview.do"
TOUR_URL = f"{BASE}/ko/8013/subview.do"
SCHOOL_SUFFIX = "kyungnam.ac.kr"
CONTACT_LINK = re.compile(r"찾아오시는|오시는\s*길|위치\s*안내|연락처|Contact", re.I)
PHONE = re.compile(r"\(?0\d{1,2}\)?[-\s]?\d{3,4}-\d{4}")
FLOOR_ROOM = r"(?:\s*(?:지하\s*)?\d{1,2}\s*층)?(?:\s*\d{2,4}\s*호(?:실)?)?"
COLLECTOR_VERSION = "places-v1"


@dataclass
class Candidate:
    name: str
    kind: str  # building | unit
    raw_location: str | None = None
    phone: str | None = None
    source_url: str | None = None
    evidence_text: str | None = None
    parent: str | None = None
    aliases: list[str] = field(default_factory=list)

    @property
    def place_id(self) -> str:
        prefix = "b" if self.kind == "building" else "u"
        return f"{prefix}-{hashlib.sha1(self.name.encode('utf-8')).hexdigest()[:12]}"


def is_school_url(url: str) -> bool:
    p = urlparse(url)
    host = (p.hostname or "").lower()
    return p.scheme == "https" and (host == SCHOOL_SUFFIX or host.endswith("." + SCHOOL_SUFFIX))


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("055") and len(digits) == 10:
        return f"055-{digits[3:6]}-{digits[6:]}"
    if len(digits) == 11:
        return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}"
    return raw.strip()


def _lines(html: str) -> tuple[BeautifulSoup, list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    root = soup.select_one("#_contentBuilder") or soup.body or soup
    return soup, [ln.strip() for ln in root.get_text("\n").splitlines() if ln.strip()]


def parse_org_chart(html: str) -> tuple[list[str], dict[str, str]]:
    """조직 이름 목록과 이름→링크(학교 도메인만)."""
    soup, lines = _lines(html)
    root = soup.select_one("#_contentBuilder") or soup
    # 조직도 본문은 한 줄에 조직 이름 하나씩이다(실측) — 긴 줄·머리글은 뺀다
    names = list(dict.fromkeys(n for n in lines if len(n) <= 30 and "컨텐츠" not in n))
    links: dict[str, str] = {}
    for a in root.find_all("a", href=True):
        url = urljoin(BASE, a["href"])
        text = a.get_text(strip=True)
        if text and is_school_url(url):
            links.setdefault(text, url)
    return names, links


def parse_tour(html: str) -> list[str]:
    names = re.findall(r"jf_sel\([^)]*\)[^>]*>([^<]+)<", html)
    skip = {"전경", "정문게시판", "산복도로진입로"}
    return [n.strip() for n in dict.fromkeys(names) if n.strip() and n.strip() not in skip]


def location_pattern(buildings: Sequence[str]) -> re.Pattern[str]:
    names = sorted(
        {b for b in buildings if b.endswith(("관", "도서관", "생활관"))} | {"본관"},
        key=len,
        reverse=True,
    )
    alt = "|".join(re.escape(n) for n in names)
    return re.compile(rf"(?:{alt}){FLOOR_ROOM}")


def extract_unit_blocks(lines: list[str], units: set[str], loc_re: re.Pattern[str], url: str):
    """'위치 : X' / '전화 : Y' 블록을 가장 가까운 앞쪽 조직 이름에 붙인다. 직원표 행은 건너뛴다."""
    out: dict[str, Candidate] = {}
    current: str | None = None
    for i, line in enumerate(lines):
        if "○○" in line or "○" in line:
            continue  # 직원 개인 행
        if line in units:
            current = line
            continue
        if current is None:
            continue
        value = (
            lines[i + 1].lstrip(": ").strip()
            if line in ("위치", "전화") and i + 1 < len(lines)
            else ""
        )
        if line == "위치" and value:
            m = loc_re.search(value)
            if m:
                c = out.setdefault(current, Candidate(current, "unit", source_url=url))
                c.raw_location = c.raw_location or m.group(0).strip()
                c.evidence_text = f"{current} 위치 : {value}"[:200]
        elif line == "전화" and value:
            m = PHONE.search(value)
            if m:
                c = out.setdefault(current, Candidate(current, "unit", source_url=url))
                c.phone = c.phone or normalize_phone(m.group(0))
                c.evidence_text = (c.evidence_text or f"{current}") + f" / 전화 : {value[:60]}"
    return list(out.values())


def extract_contact_page(unit: str, lines: list[str], loc_re: re.Pattern[str], url: str):
    """하위 사이트 '찾아오시는 길' 페이지: 첫 건물 위치 문구와 첫 대표 전화."""
    text = " ".join(ln for ln in lines if "○" not in ln)
    loc = loc_re.search(text)
    phone = PHONE.search(text)
    if not loc and not phone:
        return None
    ev_at = (loc or phone).start()  # type: ignore[union-attr]
    return Candidate(
        unit,
        "unit",
        raw_location=loc.group(0).strip() if loc else None,
        phone=normalize_phone(phone.group(0)) if phone else None,
        source_url=url,
        evidence_text=text[max(0, ev_at - 60) : ev_at + 80],
    )


def extract_pairs(lines: list[str], units: set[str], loc_re: re.Pattern[str], url: str):
    """찾아오시는길 페이지의 "총무인사팀 : 본관 1층" / "한마관 5층 : 장학복지팀" 문장만 짝짓기."""
    out: list[Candidate] = []
    for line in lines:
        if ":" not in line or "○" in line:
            continue
        left, right = (x.strip() for x in line.split(":", 1))
        for loc_side, name_side in ((right, left), (left, right)):
            m = loc_re.fullmatch(loc_side) or loc_re.match(loc_side)
            if not m:
                continue
            names = [n.strip() for n in re.split(r"[,·/]", name_side) if n.strip()]
            for name in names:
                if name in units:
                    out.append(
                        Candidate(
                            name,
                            "unit",
                            raw_location=m.group(0).strip(),
                            source_url=url,
                            evidence_text=line[:200],
                        )
                    )
            break
    return out


class Fetcher:
    def __init__(self, client: httpx.AsyncClient, interval: float = 1.0, max_pages: int = 200):
        self.client, self.interval, self.max_pages = client, interval, max_pages
        self.count = 0

    async def get(self, url: str) -> str | None:
        if not is_school_url(url) or self.count >= self.max_pages:
            return None
        self.count += 1
        await asyncio.sleep(self.interval)
        try:
            r = await self.client.get(url)
        except httpx.HTTPError:
            return None
        if r.status_code in (403, 429):
            raise RuntimeError(f"collection stopped: HTTP {r.status_code} at {url}")
        if r.status_code != 200 or not is_school_url(str(r.url)):
            return None
        return r.text


async def collect(fetcher: Fetcher) -> dict[str, object]:
    org_html = await fetcher.get(ORG_URL)
    tour_html = await fetcher.get(TOUR_URL)
    if not org_html or not tour_html:
        raise RuntimeError("org chart or campus tour unavailable")
    units, links = parse_org_chart(org_html)
    buildings = parse_tour(tour_html)
    loc_re = location_pattern(buildings)
    unit_set = set(units)
    found: dict[str, Candidate] = {
        b: Candidate(
            b,
            "building",
            raw_location=b,
            source_url=TOUR_URL,
            evidence_text=f"캠퍼스투어 건물 목록: {b}",
        )
        for b in buildings
    }
    visited: set[str] = set()
    www_contacts: set[str] = set()
    for unit, url in links.items():
        page_url = url.split("#")[0]
        if page_url in visited:
            continue
        visited.add(page_url)
        html = await fetcher.get(page_url)
        if not html:
            continue
        soup, lines = _lines(html)
        for c in extract_unit_blocks(lines, unit_set, loc_re, page_url):
            found.setdefault(c.name, c)
        if unit in found and found[unit].raw_location:
            continue
        if urlparse(page_url).path.startswith("/ko/"):
            # 본 사이트 공통 메뉴에는 다른 부서의 찾아오시는길 링크가 섞임(실측: 총장↔장학복지팀
            # 오매칭) → 본 사이트는 링크를 따라가지 않고, 아래에서 '찾아오시는길' 페이지를 모아
            # "조직 : 위치" 문장으로만 짝을 짓는다
            www_contacts.update(
                urljoin(page_url, a["href"])
                for a in soup.find_all("a", href=True)
                if CONTACT_LINK.search(a.get_text(" ", strip=True)) and "4218" not in a["href"]
            )
            continue
        if unit in found and found[unit].raw_location:
            continue
        # 하위 사이트(자기 메뉴만 있음): 같은 호스트의 '찾아오시는 길' 1단계
        host = urlparse(page_url).hostname
        contact = next(
            (
                urljoin(page_url, a["href"])
                for a in soup.find_all("a", href=True)
                if CONTACT_LINK.search(a.get_text(" ", strip=True))
                and urlparse(urljoin(page_url, a["href"])).hostname == host
                and not urlparse(urljoin(page_url, a["href"])).path.startswith("/ko/")
            ),
            None,
        )
        if contact and contact not in visited:
            visited.add(contact)
            chtml = await fetcher.get(contact)
            if chtml:
                _, clines = _lines(chtml)
                c = extract_contact_page(unit, clines, loc_re, contact)
                if c and unit not in found:
                    found[unit] = c
    for url in sorted(www_contacts - visited):
        visited.add(url)
        html = await fetcher.get(url)
        if not html:
            continue
        _, lines = _lines(html)
        for c in extract_pairs(lines, unit_set, loc_re, url):
            prev = found.get(c.name)
            if prev is None:
                found[c.name] = c
            elif not prev.raw_location:
                prev.raw_location, prev.evidence_text = c.raw_location, c.evidence_text
    for u in units:
        if u in found and found[u].kind == "unit":
            found[u].parent = None
    return {
        "units_in_chart": len(units),
        "buildings": len(buildings),
        "pages_fetched": fetcher.count,
        "candidates": [asdict(c) | {"place_id": c.place_id} for c in found.values()],
    }


def write_pending(
    candidates: list[dict[str, object]], project: str, database: str
) -> dict[str, int]:
    from google.cloud import firestore

    db = firestore.Client(project=project, database=database)
    today = dt.date.today().isoformat()
    now = dt.datetime.now(dt.UTC)
    stats = {"written": 0, "skipped_verified": 0}
    for c in candidates:
        ref = db.collection("places").document(str(c["place_id"]))
        snap = ref.get()
        if snap.exists and (snap.to_dict() or {}).get("status") == "verified":
            stats["skipped_verified"] += 1
            continue
        row = {k: v for k, v in c.items() if v not in (None, [], "")}
        row.update(
            {
                "status": "pending",
                "snapshot_at": today,
                "collected_by": COLLECTOR_VERSION,
                "updated_at": now,
                "updated_by": COLLECTOR_VERSION,
            }
        )
        if not snap.exists:
            row["created_at"] = now
        ref.set(row, merge=True)
        stats["written"] += 1
    return stats


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Collect place/department candidates (pending)")
    p.add_argument("--output", type=Path, default=Path("data/processed/place_candidates.json"))
    p.add_argument("--write", action="store_true", help="Firestore places에 pending으로 기록")
    p.add_argument("--max-pages", type=int, default=200)
    args = p.parse_args(argv)
    contact = os.environ.get("COLLECTION_CONTACT_EMAIL")
    if not contact:
        raise SystemExit("COLLECTION_CONTACT_EMAIL is required for identifiable collection")

    async def run():
        async with httpx.AsyncClient(
            headers={"User-Agent": f"CampusBridge/0.1 (contest; contact: {contact})"},
            timeout=20,
            follow_redirects=True,
        ) as client:
            return await collect(Fetcher(client, max_pages=args.max_pages))

    result = asyncio.run(run())
    if args.write:
        result["firestore"] = write_pending(
            result["candidates"],  # type: ignore[arg-type]
            os.environ["GCP_PROJECT_ID"],
            os.environ.get("FIRESTORE_DB", "campusbridge"),
        )
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    cands = result["candidates"]
    summary = {
        "pages": result["pages_fetched"],
        "buildings": sum(1 for c in cands if c["kind"] == "building"),  # type: ignore[index]
        "units_with_location": sum(
            1 for c in cands if c["kind"] == "unit" and c.get("raw_location")
        ),  # type: ignore[index,union-attr]
        "units_with_phone": sum(1 for c in cands if c["kind"] == "unit" and c.get("phone")),  # type: ignore[index,union-attr]
        "firestore": result.get("firestore"),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

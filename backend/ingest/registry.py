"""Allowlisted Kyungnam University regulation registry collection."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from charset_normalizer import from_bytes

REGISTRY_URL = "https://yz.kyungnam.ac.kr/rule/"
ALLOWED_HOST = "yz.kyungnam.ac.kr"
PART_LINK = re.compile(r"제\s*(\d+)\s*편")
HWP_NAME = re.compile(r"/reg(\d+)_(\d+)\.hwp$", re.IGNORECASE)
REVISION_NUMBER = re.compile(r"(\d+)\s*차")


@dataclasses.dataclass(frozen=True, slots=True)
class RuleRecord:
    rule_no: str
    rule_name: str
    part: int
    part_name: str
    revision_no: int
    revision_date: str | None
    enacted_date: str | None
    department: str | None
    hwp_url: str
    file_version: str
    rule_level: str

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> RuleRecord:
        return cls(**value)  # type: ignore[arg-type]


def _decode_html(content: bytes, declared_encoding: str | None = None) -> str:
    candidates = [declared_encoding, "utf-8", "cp949", "euc-kr"]
    for encoding in candidates:
        if not encoding:
            continue
        try:
            return content.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            pass
    detected = from_bytes(content).best()
    if detected is None:
        raise UnicodeError("unable to determine registry response encoding")
    return str(detected)


def _rule_level(name: str) -> str:
    compact = name.strip()
    if compact.endswith("시행세칙") or compact.endswith("세칙"):
        return "시행세칙"
    if compact.endswith("학칙"):
        return "학칙"
    if compact.endswith("규정"):
        return "규정"
    if compact.endswith(("지침", "요령")):
        return "지침"
    return "기타"


def _assert_allowed_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != ALLOWED_HOST:
        raise ValueError(f"URL is outside the regulation allowlist: {url}")


def parse_part_links(html: str, *, base_url: str = REGISTRY_URL) -> dict[int, str]:
    soup = BeautifulSoup(html, "html.parser")
    links = {1: base_url}
    for anchor in soup.select("a[href]"):
        match = PART_LINK.search(anchor.get_text(" ", strip=True))
        if match is None:
            continue
        url = urljoin(base_url, anchor["href"])
        _assert_allowed_url(url)
        links[int(match.group(1))] = url
    return links


def parse_rule_page(html: str, *, part: int, page_url: str) -> list[RuleRecord]:
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.select_one("h2.maintitl")
    part_name = heading.get_text(" ", strip=True) if heading else f"제{part}편"
    records: list[RuleRecord] = []
    for row in soup.select("table[summary*='규정명'] tbody > tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) < 6:
            continue
        anchor = row.select_one("a[href*='/rule/rdata/'][href$='.hwp']")
        if anchor is None:
            continue
        hwp_url = urljoin(page_url, anchor.get("href", ""))
        _assert_allowed_url(hwp_url)
        hwp_match = HWP_NAME.search(urlparse(hwp_url).path)
        if hwp_match is None:
            continue
        name = cells[1].get_text(" ", strip=True)
        revision_text = cells[5].get_text(" ", strip=True)
        revision_match = REVISION_NUMBER.search(revision_text)
        records.append(
            RuleRecord(
                rule_no=hwp_match.group(1),
                rule_name=name,
                part=part,
                part_name=part_name,
                revision_no=int(revision_match.group(1)) if revision_match else 0,
                revision_date=cells[4].get_text(" ", strip=True) or None,
                enacted_date=cells[3].get_text(" ", strip=True) or None,
                department=cells[2].get_text(" ", strip=True) or None,
                hwp_url=hwp_url,
                file_version=hwp_match.group(2),
                rule_level=_rule_level(name),
            )
        )
    return records


def _user_agent() -> str:
    contact = os.getenv("COLLECTION_CONTACT_EMAIL")
    if not contact:
        raise RuntimeError("COLLECTION_CONTACT_EMAIL is required for identifiable collection")
    return f"CampusBridge/0.1 (contest; contact: {contact})"


async def _fetch_html(client: httpx.AsyncClient, url: str) -> str:
    _assert_allowed_url(url)
    response = await client.get(url)
    if response.status_code in {403, 429}:
        raise RuntimeError(f"collection stopped: registry returned HTTP {response.status_code}")
    response.raise_for_status()
    return _decode_html(response.content, response.encoding)


async def collect_rules(
    *,
    parts: Iterable[int] = (2, 3),
    min_interval_seconds: float = 1.0,
    client: httpx.AsyncClient | None = None,
) -> list[RuleRecord]:
    """Collect current rules for selected parts, without recursive crawling."""

    wanted = tuple(dict.fromkeys(parts))
    if any(part < 1 or part > 7 for part in wanted):
        raise ValueError("parts must be in the registry range 1..7")
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            headers={"User-Agent": _user_agent()},
            timeout=httpx.Timeout(20.0),
            follow_redirects=True,
        )
    try:
        root_html = await _fetch_html(client, REGISTRY_URL)
        part_links = parse_part_links(root_html)
        records: list[RuleRecord] = []
        for index, part in enumerate(wanted):
            url = part_links.get(part)
            if url is None:
                raise RuntimeError(f"registry did not expose a link for part {part}")
            html = root_html if url == REGISTRY_URL else await _fetch_html(client, url)
            records.extend(parse_rule_page(html, part=part, page_url=url))
            if index < len(wanted) - 1 and min_interval_seconds > 0:
                await asyncio.sleep(min_interval_seconds)
        return records
    finally:
        if owns_client:
            await client.aclose()


def write_rules(records: Sequence[RuleRecord], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = [record.to_dict() for record in records]
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_rules(path: Path) -> list[RuleRecord]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("rules file root must be an array")
    return [RuleRecord.from_dict(item) for item in payload]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect the current regulation registry")
    parser.add_argument("--parts", nargs="+", type=int, default=[2, 3])
    parser.add_argument("--output", type=Path, default=Path("data/processed/rules.json"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    records = asyncio.run(collect_rules(parts=args.parts))
    write_rules(records, args.output)
    print(f"rules={len(records)} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

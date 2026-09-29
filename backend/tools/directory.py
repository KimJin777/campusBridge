"""Deterministic lookups for reviewed department and campus-place data."""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any

from backend.app.config import Settings
from backend.domain import Dept, Evidence, ToolResult
from backend.tools.common import ensure_allowed_url, truncate, utc_now

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


def _read_rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [
            {str(key).strip(): (value or "").strip() for key, value in row.items()}
            for row in csv.DictReader(handle)
        ]


def _first(row: dict[str, str], *keys: str) -> str:
    return next((row[key] for key in keys if row.get(key)), "")


def _dept_from_row(row: dict[str, str]) -> Dept | None:
    dept_id = _first(row, "dept_id", "id")
    name = _first(row, "name", "department", "dept_name")
    if not dept_id or not name:
        return None
    return Dept(
        dept_id=dept_id,
        name=name,
        phone=_first(row, "phone", "telephone") or None,
        duties=_first(row, "duties", "duty", "description") or None,
        location_text=_first(row, "location_text", "location") or None,
        source_url=_first(row, "source_url", "url") or None,
        snapshot_at=_first(row, "snapshot_at", "as_of") or None,
    )


def dept_lookup(dept_id: str, *, path: Path | None = None) -> Dept | None:
    """Return a reviewed department record; missing or malformed rows are ignored."""

    target = truncate(dept_id, 100)
    if not target:
        return None
    for row in _read_rows(path or DATA_DIR / "department_directory.csv"):
        if _first(row, "dept_id", "id") == target:
            return _dept_from_row(row)
    return None


def _normalize(value: str) -> str:
    return re.sub(r"[\s\-_.()]+", "", value).casefold()


def _aliases(row: dict[str, str]) -> set[str]:
    raw = _first(row, "aliases", "alias")
    return {_normalize(item) for item in re.split(r"[|;,]", raw) if item.strip()}


def _distance_at_most_one(left: str, right: str) -> bool:
    if abs(len(left) - len(right)) > 1:
        return False
    if left == right:
        return True
    if len(left) == len(right):
        return sum(a != b for a, b in zip(left, right, strict=True)) <= 1
    short, long = (left, right) if len(left) < len(right) else (right, left)
    index_short = index_long = differences = 0
    while index_short < len(short) and index_long < len(long):
        if short[index_short] == long[index_long]:
            index_short += 1
        else:
            differences += 1
            if differences > 1:
                return False
        index_long += 1
    return True


def _match_tier(row: dict[str, str], query: str) -> int | None:
    name = _normalize(_first(row, "name", "place_name"))
    if query == name:
        return 0
    if query in _aliases(row):
        return 1
    if query in name or name in query or _distance_at_most_one(query, name):
        return 2
    return None


def _location_text(row: dict[str, str], by_id: dict[str, dict[str, str]]) -> str:
    raw = _first(row, "raw_location", "location_text", "location")
    if raw:
        return raw
    parent_id = _first(row, "parent_place_id", "parent_id")
    parent = by_id.get(parent_id, {})
    parent_name = _first(parent, "name", "place_name")
    floor = _first(row, "floor")
    room = _first(row, "room")
    parts = [parent_name]
    if floor:
        parts.append(f"{floor}층")
    if room:
        parts.append(f"{room}호")
    return " ".join(part for part in parts if part)


async def find_campus_location(
    query: str,
    *,
    settings: Settings,
    path: Path | None = None,
) -> ToolResult:
    """Find reviewed place records by exact name, alias, then conservative fuzzy match."""

    normalized = _normalize(truncate(query, 30))
    if not normalized:
        return ToolResult.fail("BAD_INPUT", "찾을 장소 이름을 입력해 주세요")
    rows = [
        row
        for row in _read_rows(path or DATA_DIR / "places.csv")
        if _first(row, "status").casefold() == "verified"
    ]
    if not rows:
        return ToolResult.empty("검수된 장소 정보가 아직 없습니다")

    candidates: list[tuple[int, dict[str, str]]] = []
    for row in rows:
        tier = _match_tier(row, normalized)
        if tier is not None:
            candidates.append((tier, row))
    if not candidates:
        return ToolResult.empty("해당 장소를 찾지 못했습니다")
    best_tier = min(tier for tier, _ in candidates)
    matches = [row for tier, row in candidates if tier == best_tier]
    by_id = {_first(row, "place_id", "id"): row for row in rows}
    items: list[Evidence] = []
    for row in matches:
        place_id = _first(row, "place_id", "id")
        name = _first(row, "name", "place_name")
        if not place_id or not name:
            continue
        source_url = _first(row, "source_url", "url")
        try:
            source_url = (
                ensure_allowed_url(source_url, settings.allowed_hosts) if source_url else ""
            )
        except ValueError:
            source_url = ""
        location = _location_text(row, by_id)
        meta: dict[str, Any] = {
            "kind": _first(row, "kind", "place_kind") or None,
            "parent_place_id": _first(row, "parent_place_id", "parent_id") or None,
            "snapshot_at": _first(row, "snapshot_at", "as_of") or None,
        }
        items.append(
            Evidence(
                id=f"place:{place_id}",
                kind="place",
                title=name,
                text=f"{name}: {location}" if location else name,
                url=source_url or None,
                meta={key: value for key, value in meta.items() if value is not None},
            )
        )
    if not items:
        return ToolResult.empty("해당 장소를 찾지 못했습니다")
    return ToolResult(
        ok=True,
        items=items,
        message="여러 장소가 있습니다" if len(items) > 1 else None,
        as_of=utc_now(),
    )

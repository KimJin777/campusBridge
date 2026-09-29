"""Deterministic lookups for reviewed department and campus-place data."""

from __future__ import annotations

import asyncio
import csv
import re
import time
from pathlib import Path
from typing import Any

from backend.app.config import Settings
from backend.domain import Dept, Evidence, ToolResult
from backend.tools.common import is_school_url, truncate, utc_now

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
    if path is not None:
        return None
    # CSV(D16)에 없으면 ① 전화번호부(업로드 시 자동 적용) ② 검수 완료된 장소 표 조직 행
    from backend.app.config import get_settings

    settings = get_settings()
    place = next(
        (
            r
            for r in _verified_rows(settings, None)
            if _first(r, "kind") == "unit"
            and target in (_first(r, "place_id", "id"), _first(r, "name"))
        ),
        None,
    )
    book = _directory_rows(settings).get(target) or (
        _directory_rows(settings).get(_first(place, "name")) if place else None
    )
    if not place and not book:
        return None
    source = _first(place, "source_url") if place else ""
    return Dept(
        dept_id=_first(place, "place_id", "id") if place else str(book.get("id")),
        name=_first(place, "name") if place else str(book.get("name")),
        # 전화는 최신 전화번호부 우선, 위치는 검수된 장소 표에서
        phone=(book or {}).get("phone") or (_first(place, "phone") if place else None) or None,
        location_text=(_first(place, "raw_location") if place else "") or None,
        source_url=source if source and is_school_url(source) else None,
        snapshot_at=(_first(place, "snapshot_at") if place else None)
        or (str(book.get("applied_at"))[:10] if book else None),
    )


DIRECTORY_TTL_SECONDS = 300
_directory_cache: dict[str, Any] = {"at": 0.0, "rows": {}}


def _directory_rows(settings: Settings) -> dict[str, dict[str, Any]]:
    """Firestore `directory_entries`(전화번호부) 이름→행. 5분 캐시, 실패하면 직전 값."""
    if not settings.gcp_project_id:
        return {}
    now = time.monotonic()
    if now - _directory_cache["at"] < DIRECTORY_TTL_SECONDS:
        return _directory_cache["rows"]
    try:
        from google.cloud import firestore

        client = firestore.Client(project=settings.gcp_project_id, database=settings.firestore_db)
        rows = {
            (d.to_dict() or {}).get("name", ""): {"id": d.id, **(d.to_dict() or {})}
            for d in client.collection("directory_entries").stream()
        }
    except Exception:  # noqa: BLE001
        rows = _directory_cache["rows"]
    _directory_cache.update(at=now, rows=rows)
    return rows


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


MAX_TENANTS = 20


def _tenants(
    building: dict[str, str], rows: list[dict[str, str]], by_id: dict[str, dict[str, str]]
) -> list[tuple[str, str]]:
    """건물 안의 검수된 부서·시설.

    parent_place_id가 이 건물이거나 위치 문구가 건물 이름으로 시작하는 행.
    """
    bid = _first(building, "place_id", "id")
    bname = _first(building, "name", "place_name")
    if not bid or not bname:
        return []
    key = _normalize(bname)
    out: list[tuple[str, str]] = []
    for row in rows:
        rid = _first(row, "place_id", "id")
        if rid == bid:
            continue
        loc = _location_text(row, by_id)
        parent = _first(row, "parent_place_id", "parent_id")
        if parent == bid or _normalize(loc).startswith(key):
            out.append((_first(row, "name", "place_name"), loc))
    out.sort()
    return [t for t in out if t[0]][:MAX_TENANTS]


PLACES_TTL_SECONDS = 300
_places_cache: dict[str, Any] = {"at": 0.0, "rows": []}


def _firestore_place_rows(settings: Settings) -> list[dict[str, str]]:
    """Firestore `places`의 verified 행(#543: 운영 정본). 5분 캐시, 실패하면 빈 목록."""
    if not settings.gcp_project_id:
        return []
    now = time.monotonic()
    if now - _places_cache["at"] < PLACES_TTL_SECONDS:
        return _places_cache["rows"]
    try:
        from google.cloud import firestore

        client = firestore.Client(project=settings.gcp_project_id, database=settings.firestore_db)
        docs = client.collection("places").where("status", "==", "verified").stream()
        rows = [_flatten(d.id, d.to_dict() or {}) for d in docs]
    except Exception:  # noqa: BLE001 — 장소는 CSV seed로 계속 동작
        rows = _places_cache["rows"]
    _places_cache.update(at=now, rows=rows)
    return rows


def _flatten(place_id: str, data: dict[str, Any]) -> dict[str, str]:
    row = {
        k: ("|".join(v) if isinstance(v, list) else str(v))
        for k, v in data.items()
        if v is not None
    }
    row.setdefault("place_id", place_id)
    return row


def _verified_rows(settings: Settings, path: Path | None) -> list[dict[str, str]]:
    """CSV seed + Firestore 운영 행(같은 place_id면 Firestore 우선). verified만."""
    merged: dict[str, dict[str, str]] = {}
    for row in _read_rows(path or DATA_DIR / "places.csv"):
        if _first(row, "status").casefold() == "verified":
            merged[_first(row, "place_id", "id")] = row
    if path is None:
        for row in _firestore_place_rows(settings):
            merged[_first(row, "place_id", "id")] = row
    return [r for r in merged.values() if _first(r, "status").casefold() == "verified"]


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
    rows = await asyncio.to_thread(_verified_rows, settings, path)
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
    listed_buildings: set[str] = set()
    for row in matches:
        place_id = _first(row, "place_id", "id")
        name = _first(row, "name", "place_name")
        if not place_id or not name:
            continue
        source_url = _first(row, "source_url", "url")
        source_url = source_url if source_url and is_school_url(source_url) else ""
        location = _location_text(row, by_id)
        if _normalize(location) == _normalize(name):
            location = ""  # "한마관: 한마관" 같은 자기 반복 방지
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
        tenants = [] if _normalize(name) in listed_buildings else _tenants(row, rows, by_id)
        if tenants:
            listed_buildings.add(_normalize(name))
            # "한마관에는 뭐가 있나요?" — 건물 자체가 아니라 그 안의 부서·시설을 근거로 준다
            listed = ", ".join(f"{n}({loc})" if loc else n for n, loc in tenants)
            items.append(
                Evidence(
                    id=f"place:{place_id}:tenants",
                    kind="place",
                    title=f"{name}에 있는 부서·시설",
                    text=f"{name}에 있는 부서·시설: {listed}",
                    url=source_url or None,
                    meta={"kind": "building_tenants", "count": len(tenants)},
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

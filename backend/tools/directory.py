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
    rows_by_name = _directory_rows(settings)
    book = (
        rows_by_name.get(target)
        or next((r for r in rows_by_name.values() if r.get("id") == target), None)
        or (rows_by_name.get(_first(place, "name")) if place else None)
    )
    if not place and not book:
        return None
    source = _first(place, "source_url") if place else ""
    return Dept(
        dept_id=_first(place, "place_id", "id") if place else str(book.get("id")),
        name=_first(place, "name") if place else str(book.get("name")),
        # 전화는 최신 전화번호부 우선, 위치는 검수된 장소 표에서
        phone=(book or {}).get("phone") or (_first(place, "phone") if place else None) or None,
        phones=[str(n) for n in (book or {}).get("phones") or []],
        location_text=(_first(place, "raw_location") if place else "") or None,
        source_url=source if source and is_school_url(source) else None,
        snapshot_at=(_first(place, "snapshot_at") if place else None)
        or (str(book.get("applied_at"))[:10] if book else None),
    )


DIRECTORY_TTL_SECONDS = 300
# "at"은 time.monotonic() 기준. 새 인스턴스는 monotonic이 작아 0.0을 "안 읽음"으로 쓰면
# 켜진 뒤 5분간 DB를 읽지 않고 빈 값을 돌려준다(배포 직후 위치 0건의 원인) → -inf로 둔다
_directory_cache: dict[str, Any] = {"at": float("-inf"), "rows": {}}


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
    except Exception:  # noqa: BLE001 — 실패는 캐시하지 않고 직전 값(다음 호출에서 다시 조회)
        return _directory_cache["rows"]
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
    aliases = _aliases(row)
    if query in aliases or any(len(a) >= 4 and a in query for a in aliases):
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
_places_cache: dict[str, Any] = {"at": float("-inf"), "rows": []}


def _firestore_place_rows(settings: Settings) -> list[dict[str, str]]:
    """Firestore `places`의 verified 행(#543: 운영 정본). 5분 캐시, 실패하면 빈 목록."""
    if not settings.gcp_project_id:
        return []
    now = time.monotonic()
    if now - _places_cache["at"] < PLACES_TTL_SECONDS:
        return _places_cache["rows"]
    from google.cloud import firestore

    for attempt in range(2):  # 새 인스턴스의 첫 연결 실패 등 일시 오류는 한 번 더 시도
        try:
            client = firestore.Client(
                project=settings.gcp_project_id, database=settings.firestore_db
            )
            docs = client.collection("places").where("status", "==", "verified").stream()
            rows = [_flatten(d.id, d.to_dict() or {}) for d in docs]
            _places_cache.update(at=now, rows=rows)
            return rows
        except Exception:  # noqa: BLE001
            if attempt == 0:
                time.sleep(0.3)
    # 실패는 캐시하지 않는다(빈 목록이 5분간 굳던 문제). 이전 정상값이 있으면 그것을 쓴다
    return _places_cache["rows"]


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


def _book_evidence(settings: Settings, normalized_query: str) -> list[Evidence]:
    """전화번호부 부서 중 질문에 이름이 들어 있는 것(가장 긴 이름 우선, 최대 2개)."""
    hits = [
        row
        for name, row in _directory_rows(settings).items()
        if name and len(_normalize(name)) >= 3 and _normalize(name) in normalized_query
    ]
    hits.sort(key=lambda r: -len(str(r.get("name") or "")))
    out: list[Evidence] = []
    for row in hits[:2]:
        name = str(row.get("name") or "")
        phones = [str(p) for p in row.get("phones") or []] or (
            [str(row["phone"])] if row.get("phone") else []
        )
        if not phones:
            continue
        fax = f" · 팩스 {row['fax']}" if row.get("fax") else ""
        out.append(
            Evidence(
                id=f"dept:{row['id']}",
                kind="department",
                title=f"{name} 연락처(교내 전화번호부)",
                text=f"{name} 전화: {', '.join(phones)}{fax}",
                meta={"dept_id": str(row["id"]), "source_kind": "phonebook"},
            )
        )
    return out


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
    # 전화번호부(교수님 2026-10-01: 학과 사무실 = 조교 번호) — "역사학과 전화번호"에 근거로 준다
    book = [] if path is not None else await asyncio.to_thread(_book_evidence, settings, normalized)
    if not rows:
        if book:
            return ToolResult(ok=True, items=book, as_of=utc_now())
        return ToolResult.empty("검수된 장소 정보가 아직 없습니다")

    candidates: list[tuple[int, dict[str, str]]] = []
    for row in rows:
        tier = _match_tier(row, normalized)
        if tier is not None:
            candidates.append((tier, row))
    if not candidates:
        if book:
            return ToolResult(ok=True, items=book, as_of=utc_now())
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
    items += [b for b in book if b.id not in {i.id for i in items}]
    if not items:
        return ToolResult.empty("해당 장소를 찾지 못했습니다")
    return ToolResult(
        ok=True,
        items=items,
        message="여러 장소가 있습니다" if len(items) > 1 else None,
        as_of=utc_now(),
    )

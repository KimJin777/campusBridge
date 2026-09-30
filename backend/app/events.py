"""첫 화면 '오늘·이번 주 학사 일정' 카드(#596 합의) — campus_events에서 읽기만 한다.

오늘부터 1주일(7일) 안에 걸친 일정을 마감 임박순으로(교수님 2026-09-30). 10분 캐시.
전체보기(calendar.html)는 month_events로 월 단위 전체를 준다.
Firestore 장애·미설정이면 빈 목록(카드를 숨긴다) — 첫 화면을 막지 않는다.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from backend.app.config import Settings

KST = timezone(timedelta(hours=9), "KST")
TTL_SECONDS = 600
LIMIT = 12
SOON_DAYS = 7

_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_month_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_client: Any = None


def pick_events(rows: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    """이번 1주일(오늘~6일 뒤)에 걸친 일정 — 진행 중이거나 7일 안에 시작·마감. 마감 임박순."""
    out = []
    for r in rows:
        if r.get("status") != "active":
            continue
        try:
            end = date.fromisoformat(r["end_date"])
            start = date.fromisoformat(r["start_date"]) if r.get("start_date") else None
        except (KeyError, TypeError, ValueError):
            continue
        if end < today:
            continue
        if start is not None and start > today + timedelta(days=SOON_DAYS - 1):
            continue  # 다음 주 이후 시작 → 전체보기에서
        left = (end - today).days
        badge = "D-day" if left == 0 else f"D-{left}" if left <= SOON_DAYS else "진행 중"
        out.append(
            {
                "id": r.get("id"),
                "title": r.get("title") or "일정",
                "start_date": start.isoformat() if start else None,
                "end_date": end.isoformat(),
                "badge": badge,
                "category": r.get("source_category"),
                "label": r.get("date_label"),
                "url": r.get("source_url"),
            }
        )
    out.sort(key=lambda e: (e["end_date"], e["title"]))
    return out[:LIMIT]


def pick_month(rows: list[dict[str, Any]], first: date, last: date) -> list[dict[str, Any]]:
    """그 달(first~last)에 걸친 검수 완료 일정 전체, 시작일순(전체보기 달력)."""
    out = []
    for r in rows:
        if r.get("status") != "active":
            continue
        try:
            end = date.fromisoformat(r["end_date"])
            start = date.fromisoformat(r["start_date"]) if r.get("start_date") else end
        except (KeyError, TypeError, ValueError):
            continue
        if end < first or start > last:
            continue
        out.append(
            {
                "id": r.get("id"),
                "title": r.get("title") or "일정",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "category": r.get("source_category"),
                "label": r.get("date_label"),
                "url": r.get("source_url"),
            }
        )
    out.sort(key=lambda e: (e["start"], e["end"], e["title"]))
    return out


async def month_events(settings: Settings, ym: str) -> list[dict[str, Any]]:
    """YYYY-MM 한 달의 일정. 잘못된 형식·DB 장애는 빈 목록."""
    global _client
    try:
        y, m = (int(x) for x in ym.split("-"))
        first = date(y, m, 1)
    except ValueError:
        return []
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    key = f"month:{ym}"
    hit = _month_cache.get(key)
    if hit and time.monotonic() - hit[0] < TTL_SECONDS:
        return hit[1]
    if not settings.gcp_project_id:
        return []
    try:
        if _client is None:
            from google.cloud import firestore

            _client = firestore.AsyncClient(
                project=settings.gcp_project_id, database=settings.firestore_db
            )
        query = (
            _client.collection("campus_events")
            .where("end_date", ">=", first.isoformat())
            .limit(1000)
        )
        rows = [{"id": d.id, **(d.to_dict() or {})} async for d in query.stream()]
    except Exception:  # noqa: BLE001 — 실패하면 빈 달력
        return hit[1] if hit else []
    items = pick_month(rows, first, last)
    if len(_month_cache) > 24:
        _month_cache.clear()
    _month_cache[key] = (time.monotonic(), items)
    return items


async def today_events(settings: Settings) -> list[dict[str, Any]]:
    global _client
    today = datetime.now(KST).date()
    key = today.isoformat()
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < TTL_SECONDS:
        return hit[1]
    if not settings.gcp_project_id:
        return []
    try:
        if _client is None:
            from google.cloud import firestore

            _client = firestore.AsyncClient(
                project=settings.gcp_project_id, database=settings.firestore_db
            )
        query = _client.collection("campus_events").where("end_date", ">=", key).limit(200)
        rows = [{"id": d.id, **(d.to_dict() or {})} async for d in query.stream()]
    except Exception:  # noqa: BLE001 — 부가 카드. 실패하면 숨긴다
        return hit[1] if hit else []
    items = pick_events(rows, today)
    _cache.clear()
    _cache[key] = (time.monotonic(), items)
    return items

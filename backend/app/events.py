"""첫 화면 '오늘·이번 주 학사 일정' 카드(#596 합의) — campus_events에서 읽기만 한다.

진행 중 일정과 7일 안 마감을 마감 임박순으로 최대 5건. 10분 캐시.
Firestore 장애·미설정이면 빈 목록(카드를 숨긴다) — 첫 화면을 막지 않는다.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from backend.app.config import Settings

KST = timezone(timedelta(hours=9), "KST")
TTL_SECONDS = 600
LIMIT = 5
SOON_DAYS = 7

_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_client: Any = None


def pick_events(rows: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    """진행 중(시작 ≤ 오늘 ≤ 끝) 또는 7일 안 마감만, 마감 임박순 상위 5건."""
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
        ongoing = start is None or start <= today
        soon = end <= today + timedelta(days=SOON_DAYS)
        if not (ongoing or soon):
            continue
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

"""승인된 학생 꿀팁 찾기(교수님 #697~#715) — 공식 색인과 분리된 Firestore 전용 도구.

- 승인(관리자 확인·학생 확인)된 꿀팁만. 검증 중·가림·반려는 절대 쓰지 않는다.
- 위치(location) 질문에서만 호출된다(needs.plan_calls가 서버 규칙으로 강제).
- 근거 종류는 tip — 답변 카드에 "학생 제보 꿀팁 · 학교 공식 정보 아님" 라벨.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from backend.app.config import Settings
from backend.domain.evidence import Evidence, ToolResult

APPROVED = ("approved", "student_approved")
TTL_SECONDS = 300
MAX_TIPS = 3
TIP_LABEL = "학생 제보 꿀팁 · 학교 공식 정보 아님"
_cache: dict[str, Any] = {"at": 0.0, "rows": []}
STOP = {"어디", "어디야", "어디에", "있어", "있나요", "가는", "가려면", "위치", "알려줘", "어떻게"}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[가-힣A-Za-z0-9]{2,}", text or "") if w not in STOP}


def _rows(settings: Settings) -> list[dict[str, Any]]:
    now = time.monotonic()
    if now - _cache["at"] < TTL_SECONDS:
        return _cache["rows"]
    try:
        from google.cloud import firestore
        from google.cloud.firestore_v1.base_query import FieldFilter

        db = firestore.Client(project=settings.gcp_project_id, database=settings.firestore_db)
        q = db.collection("reports").where(filter=FieldFilter("type", "==", "tip"))
        rows = [
            {"id": d.id, **(d.to_dict() or {})}
            for d in q.stream()
            if (d.to_dict() or {}).get("status") in APPROVED
        ]
    except Exception:  # noqa: BLE001 — 꿀팁은 보조 근거. 실패하면 없는 것으로
        rows = _cache["rows"]
    _cache.update(at=now, rows=rows)
    return rows


def match_tips(query: str, rows: list[dict[str, Any]], places: set[str]) -> list[dict[str, Any]]:
    """질문에 나온 장소 이름이 꿀팁에 있거나, 핵심 낱말이 2개 이상 겹치는 꿀팁."""
    q_words = _words(query)
    q_places = {p for p in places if p in (query or "")}
    scored = []
    for r in rows:
        text = r.get("published_text") or r.get("text_masked") or ""
        shared_places = sum(1 for p in q_places if p in text)
        shared_words = len(q_words & _words(text))
        score = shared_places * 3 + shared_words
        if shared_places or shared_words >= 2:
            scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored[:MAX_TIPS]]


def _place_names() -> set[str]:
    try:
        from backend.tools.campus_route import load

        return set(load()["places"])
    except Exception:  # noqa: BLE001
        return set()


async def find_campus_tips(query: str, *, settings: Settings) -> ToolResult:
    if not settings.gcp_project_id:
        return ToolResult.empty("승인된 꿀팁이 없습니다")
    rows = await asyncio.to_thread(_rows, settings)
    hits = match_tips(query, rows, _place_names())
    if not hits:
        return ToolResult.empty("관련 꿀팁이 없습니다")
    items = [
        Evidence(
            id=f"tip:{r['id']}",
            kind="tip",
            title=TIP_LABEL,
            text=r.get("published_text") or r.get("text_masked") or "",
            url="/tips.html",
            meta={
                "label": TIP_LABEL,
                "badge": "관리자 확인" if r.get("status") == "approved" else "학생 확인",
            },
        )
        for r in hits
    ]
    return ToolResult(ok=True, items=items)

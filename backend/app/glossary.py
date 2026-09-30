"""경남대 고유 용어 사전(교수님 #767: 너른마당·월영지처럼 학교에만 있는 말).

- 기본 용어는 config/glossary.yml `campus_terms`, 관리자가 화면에서 더하거나 고친 것은
  Firestore `glossary_terms`(같은 용어면 관리자 것이 우선, status=deleted면 숨김).
- 질문에 용어(또는 다른 이름)가 나오면 '용어: 뜻' 한 줄을 질문 분석·답변 작성에 참고로 넣는다.
  근거(인용)로 쓰지는 않는다 — 답변 인용은 여전히 학교 원문만.
- 5분 캐시. Firestore 장애면 마지막 성공값 또는 기본 용어(실패는 캐시하지 않는다).
"""

from __future__ import annotations

import time
import unicodedata
from pathlib import Path
from typing import Any

from backend.app.config import Settings

TTL_SECONDS = 300
MAX_HINTS = 5
CONFIG = Path(__file__).resolve().parents[2] / "config" / "glossary.yml"

_state: dict[str, Any] = {"at": float("-inf"), "terms": None}
_client: Any = None


def _norm(text: str) -> str:
    return "".join(unicodedata.normalize("NFC", text or "").split()).lower()


def seed_terms(path: Path = CONFIG) -> list[dict[str, Any]]:
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return []
    out = []
    for t in data.get("campus_terms") or []:
        if isinstance(t, dict) and t.get("term") and t.get("meaning"):
            out.append(
                {
                    "term": str(t["term"]),
                    "aliases": [str(a) for a in t.get("aliases") or []],
                    "meaning": str(t["meaning"]),
                    "source": "기본",
                }
            )
    return out


def merge_terms(seeds: list[dict[str, Any]], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by = {_norm(t["term"]): t for t in seeds}
    for r in rows:
        key = _norm(str(r.get("term") or ""))
        if not key:
            continue
        if r.get("status") == "deleted":
            by.pop(key, None)
            continue
        by[key] = {
            "term": str(r["term"]),
            "aliases": [str(a) for a in r.get("aliases") or []],
            "meaning": str(r.get("meaning") or ""),
            "source": "관리자",
        }
    return sorted((t for t in by.values() if t["meaning"]), key=lambda t: t["term"])


def hints(query: str, terms: list[dict[str, Any]]) -> list[str]:
    q = _norm(query)
    out = []
    for t in terms:
        names = [t["term"], *t.get("aliases", [])]
        if any(_norm(n) and _norm(n) in q for n in names):
            out.append(f"{t['term']}: {t['meaning']}")
    return out[:MAX_HINTS]


def current_terms() -> list[dict[str, Any]]:
    """캐시된 용어(없으면 기본 용어) — 그래프 노드가 동기로 부른다."""
    return _state["terms"] if _state["terms"] is not None else seed_terms()


def term_hints(query: str) -> list[str]:
    return hints(query, current_terms())


async def refresh(settings: Settings) -> None:
    """턴 시작 전에 부른다. 5분 안이면 아무것도 안 한다."""
    global _client
    if time.monotonic() - _state["at"] < TTL_SECONDS:
        return
    seeds = seed_terms()
    if not settings.gcp_project_id:
        _state.update(at=time.monotonic(), terms=seeds)
        return
    try:
        if _client is None:
            from google.cloud import firestore

            _client = firestore.AsyncClient(
                project=settings.gcp_project_id, database=settings.firestore_db
            )
        rows = [d.to_dict() or {} async for d in _client.collection("glossary_terms").stream()]
    except Exception:  # noqa: BLE001 — 실패는 캐시하지 않고 기존 값 유지
        return
    _state.update(at=time.monotonic(), terms=merge_terms(seeds, rows))


def invalidate() -> None:
    _state["at"] = float("-inf")

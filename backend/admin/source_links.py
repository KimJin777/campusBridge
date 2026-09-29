"""데이터 출처별 원본 링크(관리자 '데이터 출처' 탭 표시용).

원본은 config/sources.yaml(수집 허용 목록)이다. Firestore source_configs 문서 ID와 yaml 키가
일부 다르므로 여기서 맞춘다. 읽기 실패 시 빈 목록(화면 표시만 빠지고 기능은 그대로).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

BASE_URL = "https://www.kyungnam.ac.kr"
CONFIG = Path("config/sources.yaml")


def _url(value: str) -> str:
    return value if value.startswith("https://") else f"{BASE_URL}{value}"


@lru_cache(maxsize=1)
def source_links(path: Path = CONFIG) -> dict[str, list[dict[str, str]]]:
    try:
        cfg: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))["sources"]
    except Exception:  # noqa: BLE001 — 표시용 부가 정보
        return {}
    out: dict[str, list[dict[str, str]]] = {}
    if rule := cfg.get("rule_registry", {}).get("base_url"):
        out["rules"] = [{"label": "규정집", "url": _url(rule)}]
    out["academic_guides"] = [
        {"label": p["id"], "url": _url(p["path"])}
        for p in cfg.get("academic_guides", {}).get("pages", [])
    ]
    out["notices"] = [
        {"label": f"{name} RSS", "url": _url(feed)}
        for name, feed in cfg.get("notices", {}).get("feeds", {}).items()
    ]
    if cal := cfg.get("academic_calendar", {}).get("url"):
        out["academic_calendar"] = [{"label": "학사일정", "url": _url(cal)}]
    out["menus"] = [
        {"label": p["id"], "url": _url(p["path"])} for p in cfg.get("menus", {}).get("pages", [])
    ]
    return {k: v for k, v in out.items() if v}

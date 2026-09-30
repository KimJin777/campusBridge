"""정문 출발 도보 길안내(교수님 #627·#650 — 교수님이 표시한 도보길 기반).

데이터: backend/data/campus_paths.json
(scripts/build_campus_paths.py로 생성, 기준 지도 픽셀 좌표 ≈ 1m).
건물·정문은 가장 가까운 길 위의 점(간선 투영)에 붙이고 최단경로(다익스트라)를 구한다.
외부 지도 API를 쓰지 않는다 — 화면은 이 좌표로 자체 SVG를 그린다
(카카오 키는 보류, 교수님 2026-09-30).
"""

from __future__ import annotations

import heapq
import json
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

DATA = Path(__file__).resolve().parents[1] / "data" / "campus_paths.json"
WALK_M_PER_MIN = 67.0  # 약 4km/h

# 부르는 이름 → 지도 이름
ALIASES = {
    "도서관": "중앙도서관",
    "본관": "본관",
    "대학본부": "본관",
    "경영관": "제1경영관",
    "공학관": "제1공학관",
    "생활관": "제3한마생활관",
    "기숙사": "제3한마생활관",
    "운동장": "대운동장",
    "은행": "BNK경남은행",
}
GATES = ("정문", "서문", "북문")  # 출발지 선택 목록 맨 앞(동문 2곳은 좌표 확보 후)

# 제2공학관 위쪽(오르막) 건물 — 학생들이 자주 쓰는 엘리베이터 길(교수님 2026-09-30)
UPPER_BUILDINGS = frozenset({"혁신융합관", "성훈관", "건강과학관", "보건의료관"})
ELEVATOR_TIP = (
    "오르막을 줄이려면 제1공학관 2층을 거쳐 제2공학관 2층 엘리베이터로 올라가는 길도 있습니다"
    "(학생들이 자주 이용)."
)
ORIGIN_RE = re.compile(r"(\S+?)\s*에서")


@lru_cache(maxsize=1)
def load() -> dict[str, Any]:
    return json.loads(DATA.read_text(encoding="utf-8"))


def _norm(s: str) -> str:
    return re.sub(r"[\s.()·]", "", s)


def resolve_place(text: str) -> str | None:
    """문장(장소 이름·위치 문구)에서 지도에 있는 건물 이름을 찾는다. 가장 긴 이름 우선."""
    places = load()["places"]
    hay = _norm(text or "")
    names = sorted((n for n in places if n != load()["start"]), key=len, reverse=True)
    for name in names:
        if _norm(name) in hay:
            return name
    for alias, name in sorted(ALIASES.items(), key=lambda kv: -len(kv[0])):
        if alias in (text or "") and name in places:
            return name
    return None


def resolve_origin(text: str) -> tuple[str | None, str]:
    """'한마관에서 본관 가는 길' → ("한마관", "본관 가는 길"). 출발지가 없으면 (None, 원문)."""
    for m in ORIGIN_RE.finditer(text or ""):
        name = resolve_place(m.group(1))
        if name:
            return name, (text[: m.start()] + text[m.end() :]).strip()
    return None, text


def start_choices() -> list[str]:
    """출발지 선택 목록: 문(정문·서문·북문) 먼저, 나머지 건물은 가나다순."""
    places = load()["places"]
    return [g for g in GATES if g in places] + sorted(p for p in places if p not in GATES)


def _project(
    p: tuple[float, float], a: list[int], b: list[int]
) -> tuple[float, tuple[float, float]]:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    t = (
        0.0
        if dx == dy == 0
        else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / (dx * dx + dy * dy)))
    )
    q = (ax + t * dx, ay + t * dy)
    return math.dist(p, q), q


def _snap(xy: list[float]) -> tuple[int, float, list[list[float]], list[list[float]]]:
    """가장 가까운 간선 위의 점: (간선 번호, 시작 노드 쪽 길이, 시작→점 꺾은선, 점→끝 꺾은선)."""
    data = load()
    best = None
    for ei, (_a, _b, _len, poly) in enumerate(data["edges"]):
        for si in range(len(poly) - 1):
            d, q = _project((xy[0], xy[1]), poly[si], poly[si + 1])
            if best is None or d < best[0]:
                best = (d, ei, si, q)
    _, ei, si, q = best
    poly = data["edges"][ei][3]
    head = [*poly[: si + 1], [q[0], q[1]]]
    tail = [[q[0], q[1]], *poly[si + 1 :]]
    return ei, _polylen(head), head, tail


def _polylen(poly: list[list[float]]) -> float:
    return sum(math.dist(poly[i], poly[i + 1]) for i in range(len(poly) - 1))


def _oriented(ei: int, frm: int) -> list[list[float]]:
    a, b, _len, poly = load()["edges"][ei]
    nodes = load()["nodes"]
    fwd = math.dist(poly[0], nodes[a]) <= math.dist(poly[0], nodes[b])
    seq = poly if fwd else poly[::-1]  # 꺾은선을 a→b 방향으로 맞춤
    return seq if frm == a else seq[::-1]


def route(dest_text: str, start: str | None = None) -> dict[str, Any] | None:
    """도보 경로. start가 없으면 문장 속 'OO에서'를, 그것도 없으면 정문을 출발지로 쓴다."""
    data = load()
    if start is None:
        start, dest_text = resolve_origin(dest_text)
    start = start or data["start"]
    dest = resolve_place(dest_text)
    if not dest or start not in data["places"] or dest == start:
        return None
    scale = float(data["meters_per_px"])
    s_edge, s_off, s_head, s_tail = _snap(data["places"][start]["xy"])
    d_edge, d_off, d_head, d_tail = _snap(data["places"][dest]["xy"])

    adj: dict[Any, list[tuple[Any, float, Any]]] = {}

    def link(u, v, w, how):
        adj.setdefault(u, []).append((v, w, how))
        adj.setdefault(v, []).append((u, w, how))

    for ei, (a, b, length, _poly) in enumerate(data["edges"]):
        if ei not in (s_edge, d_edge):
            link(a, b, length / scale, ("edge", ei))
    for tag, ei, off, _head, tail in (
        ("S", s_edge, s_off, s_head, s_tail),
        ("D", d_edge, d_off, d_head, d_tail),
    ):
        a, b, *_ = data["edges"][ei]
        link(tag, a, off, ("head", tag))
        link(tag, b, _polylen(tail), ("tail", tag))
    if s_edge == d_edge:  # 같은 길 위
        link("S", "D", abs(s_off - d_off), ("same",))
    geo = {"S": (s_head, s_tail), "D": (d_head, d_tail)}

    dist: dict[Any, float] = {"S": 0.0}
    prev: dict[Any, tuple[Any, Any]] = {}
    pq: list[tuple[float, str, Any]] = [(0.0, "S", "S")]
    while pq:
        d, _k, u = heapq.heappop(pq)
        if u == "D":
            break
        if d > dist.get(u, math.inf):
            continue
        for v, w, how in adj.get(u, []):
            nd = d + w
            if nd < dist.get(v, math.inf):
                dist[v], prev[v] = nd, (u, how)
                heapq.heappush(pq, (nd, str(v), v))
    if "D" not in dist:
        return None

    hops = []
    cur = "D"
    while cur != "S":
        u, how = prev[cur]
        hops.append((u, cur, how))
        cur = u
    # 건물·정문 좌표 ↔ 길 위 투영점 연결 구간도 선과 거리에 넣는다(GPT5 #667-4)
    s_xy, d_xy = data["places"][start]["xy"], data["places"][dest]["xy"]
    s_q, d_q = s_head[-1], d_head[-1]
    line: list[list[float]] = [list(s_xy)]
    for u, _v, how in reversed(hops):
        if how[0] == "edge":
            seg = _oriented(how[1], u)
        elif how[0] == "same":
            s_pt, d_pt = s_head[-1], d_head[-1]
            seg = [s_pt, d_pt]
        else:
            tag = how[1]
            head, tail = geo[tag]
            # 가상점(tag) ↔ 간선 끝 노드 구간
            part = head[::-1] if how[0] == "head" else tail
            seg = part if u == tag else part[::-1]
        line.extend(seg if len(line) > 1 else [s_q, *seg[1:]])
    line.append(list(d_xy))
    meters = (math.dist(s_xy, s_q) + dist["D"] + math.dist(d_q, d_xy)) * scale
    tips = [ELEVATOR_TIP] if dest in UPPER_BUILDINGS and start not in UPPER_BUILDINGS else []
    return {
        "from": start,
        "to": dest,
        "distance_m": round(meters),
        "minutes": max(1, math.ceil(meters / WALK_M_PER_MIN)),
        "line": [[round(x, 1), round(y, 1)] for x, y in line],
        "tips": tips,
    }


def campus_map() -> dict[str, Any]:
    """화면용 캠퍼스 약도: 모든 도보길 꺾은선과 장소 위치(좌표 크기 포함)."""
    data = load()
    return {
        "size": data["size"],
        "paths": [e[3] for e in data["edges"]],
        "places": {k: {"xy": v["xy"], "photo": v.get("photo")} for k, v in data["places"].items()},
        "start": data["start"],
        "starts": start_choices(),
        "photo_credit": data.get("photo_credit"),
    }

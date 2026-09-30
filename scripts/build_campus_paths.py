# ruff: noqa: E501 — 건물 좌표 표·설명 문장은 줄바꿈하지 않는다
"""교수님이 그린 도보길(map/도보길2.jpg, 빨간 선) → 캠퍼스 도보 그래프(backend/data/campus_paths.json).

일회성 도구(런타임 의존성 아님):
    uv run --with opencv-python-headless --with numpy --with scikit-image python scripts/build_campus_paths.py

1) 도보길 사진을 기준 지도 캡처(map/KakaoMap_20260930_094321.png)에 SIFT+RANSAC으로 정합(호모그래피)
2) 빨간 선 → 중심선(skeleton) → 갈림길·끝점 노드와 간선(길이·꺾은선)
3) 건물 위치(기준 지도 픽셀, 사람이 확인한 값)와 정문을 가장 가까운 노드에 붙인다
좌표계는 기준 지도 픽셀(1px ≈ 1m, 카카오 지도 축척 기준 근사). 화면은 이 좌표로 SVG를 그린다.
"""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
from skimage.morphology import skeletonize

ROOT = Path(__file__).resolve().parents[1]
MAP = ROOT / "map"
OUT = ROOT / "backend" / "data" / "campus_paths.json"
METERS_PER_PX = 1.0

# 기준 지도(1536×864) 위 건물 이름 위치 — 지도 라벨을 보고 사람이 찍은 값(관리자 화면에서 보정 가능하게 확장 예정)
BUILDINGS: dict[str, tuple[int, int]] = {
    "중앙도서관": (345, 494), "법정관": (276, 493), "제1경영관": (338, 415), "제2경영관": (367, 532),
    "한마관": (298, 311), "한마미래관": (416, 261), "박물관": (413, 292), "대학일자리센터": (425, 328),
    "창조관": (421, 381), "제3한마생활관": (393, 90), "문무관": (418, 122), "체육관": (393, 197),
    "화영운동장": (475, 169), "예술관": (566, 123), "미술교육관": (560, 176), "디자인관": (509, 98),
    "본관": (500, 533), "고운관": (529, 585), "10.18광장": (578, 500), "월영지": (640, 531),
    "교육관": (318, 560), "제5공학관": (351, 644), "제4공학관": (419, 677), "제1공학관": (612, 665),
    "제2공학관": (580, 713), "성훈관": (490, 721), "혁신융합관": (535, 734), "보건의료관": (487, 787),
    "대운동장": (438, 537), "테니스장": (252, 436), "BNK경남은행": (534, 470),
    "너른마당": (387, 363),  # 창조관 위 도보길의 광장 — 학생들이 부르는 이름(교수님 2026-09-30)
}
GATES: dict[str, tuple[int, int]] = {"정문": (770, 520)}  # 월영광장 쪽(교수님 2026-09-30)
TOUR = ROOT / "backend" / "data" / "campus_tour.json"  # 학교 캠퍼스투어(건물 좌표·사진, 출처: 경남대학교 홈페이지)
PHOTO_DIR = ROOT / "frontend" / "campus"
# 캠퍼스투어 이름 → 길안내 이름(시설·안내물은 제외)
TOUR_RENAME = {"서문(IC수위실)": "서문", "북문(후문)": "북문"}
TOUR_SKIP = {"전경", "정문게시판", "정문수위실", "산복도로진입로", "한마상", "팔각정"}


def read(path: Path) -> np.ndarray:
    return cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)


def homography(photo: np.ndarray, base: np.ndarray) -> np.ndarray:
    g1 = cv2.resize(cv2.cvtColor(photo, cv2.COLOR_BGR2GRAY), None, fx=0.6, fy=0.6)
    g2 = cv2.cvtColor(base, cv2.COLOR_BGR2GRAY)
    sift = cv2.SIFT_create(8000)
    k1, d1 = sift.detectAndCompute(g1, None)
    k2, d2 = sift.detectAndCompute(g2, None)
    good = [a for a, b in cv2.BFMatcher().knnMatch(d1, d2, k=2) if a.distance < 0.75 * b.distance]
    src = np.float32([k1[a.queryIdx].pt for a in good]) / 0.6
    dst = np.float32([k2[a.trainIdx].pt for a in good])
    h, inliers = cv2.findHomography(src, dst, cv2.RANSAC, 4.0)
    print(f"정합: 대응점 {len(good)}, 일치 {int(inliers.sum())}")
    return h


def red_mask(img: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    m = cv2.inRange(hsv, (0, 150, 120), (6, 255, 255)) | cv2.inRange(hsv, (172, 150, 120), (180, 255, 255))
    return cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))


NB = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]


def graph_from_skeleton(sk: np.ndarray):
    pts = {(int(y), int(x)) for y, x in zip(*np.nonzero(sk), strict=True)}

    def nbrs(p):
        return [(p[0] + dy, p[1] + dx) for dy, dx in NB if (p[0] + dy, p[1] + dx) in pts]

    special = {p for p in pts if len(nbrs(p)) != 2}
    # 붙어 있는 갈림길 픽셀을 한 노드로
    node_of: dict[tuple[int, int], int] = {}
    nodes: list[list[float]] = []
    for p in special:
        if p in node_of:
            continue
        stack, members = [p], []
        node_of[p] = len(nodes)
        while stack:
            q = stack.pop()
            members.append(q)
            for r in nbrs(q):
                if r in special and r not in node_of:
                    node_of[r] = len(nodes)
                    stack.append(r)
        ys, xs = zip(*members, strict=True)
        nodes.append([round(float(np.mean(xs)), 1), round(float(np.mean(ys)), 1)])

    edges: dict[tuple[int, int, int], list] = {}
    seen: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    for p in special:
        for nxt in nbrs(p):
            if nxt in special or (p, nxt) in seen:
                continue
            path, prev, cur = [p, nxt], p, nxt
            seen.add((p, nxt))
            while cur not in special:
                step = [r for r in nbrs(cur) if r != prev and r not in path[-3:]]
                if not step:
                    break
                prev, cur = cur, step[0]
                path.append(cur)
            if cur not in special:
                continue
            seen.add((cur, path[-2]))
            a, b = node_of[p], node_of[cur]
            if a == b:
                continue
            line = np.array([[x, y] for y, x in path], dtype=np.float32).reshape(-1, 1, 2)
            simple = cv2.approxPolyDP(line, 1.5, False).reshape(-1, 2)
            length = float(np.sum(np.linalg.norm(np.diff(np.array([[x, y] for y, x in path]), axis=0), axis=1)))
            key = (min(a, b), max(a, b), round(length))
            if key not in edges:
                poly = [[int(x), int(y)] for x, y in simple]
                edges[key] = [a, b, round(length * METERS_PER_PX, 1), poly]
    return nodes, list(edges.values())


def prune_spurs(nodes, edges, min_len=10.0):
    """중심선 가지치기: 끝점에 붙은 아주 짧은 간선(선 굵기 잔가지)을 지운다."""
    while True:
        deg: dict[int, int] = {}
        for a, b, _, _ in edges:
            deg[a] = deg.get(a, 0) + 1
            deg[b] = deg.get(b, 0) + 1
        drop = [e for e in edges if e[2] < min_len and (deg[e[0]] == 1 or deg[e[1]] == 1)]
        if not drop:
            return edges
        edges = [e for e in edges if e not in drop]


def nearest(nodes, xy):
    arr = np.array(nodes)
    i = int(np.argmin(np.linalg.norm(arr - np.array(xy), axis=1)))
    return i, float(np.linalg.norm(arr[i] - np.array(xy)))


def fit_geo(places_px: dict[str, tuple[int, int]], tour: dict[str, dict]) -> np.ndarray:
    """(경도, 위도, 1) → 기준 지도 픽셀 최소제곱 아핀. 사람이 찍은 라벨 위치를 기준점으로 쓴다."""
    names = [n for n in places_px if n in tour]
    a = np.array([[tour[n]["lng"], tour[n]["lat"], 1.0] for n in names])
    b = np.array([places_px[n] for n in names], dtype=float)
    coef, *_ = np.linalg.lstsq(a, b, rcond=None)
    res = np.linalg.norm(a @ coef - b, axis=1)
    print(f"좌표 정합: 기준점 {len(names)}, 중앙 오차 {np.median(res):.1f}px")
    return coef


def to_px(coef: np.ndarray, lat: float, lng: float) -> list[int]:
    x, y = np.array([lng, lat, 1.0]) @ coef
    return [int(round(x)), int(round(y))]


def save_photos(tour_items: list[dict]) -> dict[str, str]:
    """건물마다 첫 사진 1장을 640px로 줄여 저장(출처 표기: 개인정보 처리방침·화면 캡션)."""
    import urllib.request

    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    out = {}
    for o in tour_items:
        if not o.get("photos"):
            continue
        name = f"b{o['seq']}.jpg"
        dest = PHOTO_DIR / name
        if not dest.exists():
            url = "https://www.kyungnam.ac.kr" + urllib.request.quote(o["photos"][0])
            req = urllib.request.Request(url, headers={"User-Agent": "CampusBridge/0.1"})
            data = urllib.request.urlopen(req, timeout=30).read()
            img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                continue
            scale = 640 / max(img.shape[1], 1)
            if scale < 1:
                img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 78])[1].tofile(str(dest))
        out[o["name"]] = f"campus/{name}"
    return out


def main() -> None:
    base = read(MAP / "KakaoMap_20260930_094321.png")
    h = homography(read(MAP / "도보길.jpg"), base)
    warped = cv2.warpPerspective(red_mask(read(MAP / "도보길2.jpg")), h, (base.shape[1], base.shape[0]))
    sk = skeletonize(warped > 0)
    nodes, edges = graph_from_skeleton(sk)
    edges = prune_spurs(nodes, edges)
    used = sorted({a for a, *_ in edges} | {b for _, b, *_ in edges})
    remap = {old: i for i, old in enumerate(used)}
    nodes = [nodes[i] for i in used]
    edges = [[remap[a], remap[b], length, poly] for a, b, length, poly in edges]
    tour_items = json.loads(TOUR.read_text(encoding="utf-8"))
    tour = {o["name"]: o for o in tour_items if o.get("lat")}
    coef = fit_geo({**GATES, **BUILDINGS}, tour)
    photos = save_photos(tour_items)
    positions: dict[str, dict] = {}
    for tname, o in tour.items():  # 학교 공식 좌표 우선
        if tname in TOUR_SKIP:
            continue
        name = TOUR_RENAME.get(tname, tname)
        positions[name] = {"xy": to_px(coef, o["lat"], o["lng"]), "lat": o["lat"], "lng": o["lng"], "photo": photos.get(tname)}
    for name, xy in BUILDINGS.items():  # 투어에 없는 곳만 사람 표시 위치
        positions.setdefault(name, {"xy": list(xy)})
    places = {}
    for name, info in positions.items():
        node, dist = nearest(nodes, info["xy"])
        places[name] = {**info, "node": node, "snap_m": round(dist * METERS_PER_PX)}
    data = {
        "version": 1,
        "source": "교수님 도보길 표시(map/도보길2.jpg) → 기준 지도 픽셀 좌표",
        "size": [int(base.shape[1]), int(base.shape[0])],
        "meters_per_px": METERS_PER_PX,
        "start": "정문",
        "nodes": nodes,
        "edges": edges,
        "places": places,
        "geo": {"lnglat_to_px": coef.tolist()},  # 카카오맵 연동 시 역변환에 사용
        "photo_credit": "사진 출처: 경남대학교 홈페이지 캠퍼스투어 · 저작권: 경남대학교",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    far = {k: v["snap_m"] for k, v in places.items() if v["snap_m"] > 40}
    print(f"노드 {len(nodes)}, 간선 {len(edges)}, 장소 {len(places)} → {OUT}")
    print("길에서 40m 넘게 떨어진 장소(확인 필요):", far)


if __name__ == "__main__":
    main()

from backend.tools.campus_route import campus_map, resolve_place, route


def test_route_from_main_gate_follows_graph():
    r = route("중앙도서관은 어디에 있어?")
    assert r and r["from"] == "정문" and r["to"] == "중앙도서관"
    assert 300 < r["distance_m"] < 1500 and r["minutes"] >= 5
    assert len(r["line"]) > 5
    ends = r["line"][0], r["line"][-1]
    assert ends[0] != ends[1]


def test_route_line_and_distance_reach_building_coordinates():
    """경로 끝점이 건물 좌표와 같고, 연결 구간이 거리에 포함된다(GPT5 #667-4)."""
    import math

    from backend.tools.campus_route import load

    places = load()["places"]
    r = route("한마관")
    assert r["line"][0] == [round(v, 1) for v in places["정문"]["xy"]]
    assert r["line"][-1] == [round(v, 1) for v in places["한마관"]["xy"]]
    drawn = sum(math.dist(r["line"][i], r["line"][i + 1]) for i in range(len(r["line"]) - 1))
    assert abs(drawn * load()["meters_per_px"] - r["distance_m"]) <= r["distance_m"] * 0.03


def test_every_place_route_ends_at_its_coordinates_within_3pct():
    """전 장소: 끝점 일치, 그린 선 길이와 거리 3% 이내(간선 len_m은 원 골격 기준 — GPT5 #675)."""
    import math

    from backend.tools.campus_route import load

    data = load()
    for name, info in data["places"].items():
        if name == data["start"]:
            continue
        r = route(name)
        assert r and r["line"][-1] == [round(v, 1) for v in info["xy"]], name
        pts = r["line"]
        drawn = sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
        tol = max(3, r["distance_m"] * 0.03)
        assert abs(drawn * data["meters_per_px"] - r["distance_m"]) <= tol, name


def test_resolve_from_unit_location_and_aliases():
    assert resolve_place("학사관리팀: 본관 1층") == "본관"
    assert resolve_place("장학복지팀: 한마관 5층") == "한마관"  # 한마관 ⊂ 한마미래관 오인 없이
    assert resolve_place("한마미래관 가는 길") == "한마미래관"
    assert resolve_place("도서관 가려면") == "중앙도서관"
    assert resolve_place("오늘 학식 메뉴") is None
    assert route("없는 건물") is None


def test_campus_map_has_paths_places_and_credit():
    m = campus_map()
    assert len(m["paths"]) > 50 and "정문" in m["places"]
    assert "경남대학교" in (m["photo_credit"] or "")

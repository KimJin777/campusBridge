from backend.ingest.places_collect import (
    extract_pairs,
    extract_unit_blocks,
    is_school_url,
    location_pattern,
    normalize_phone,
    parse_tour,
)

LOC = location_pattern(["본관", "한마관", "국제어학관", "창조관", "중앙도서관"])
UNITS = {"학사관리팀", "장학복지팀", "시설관리팀", "구매관재팀", "총장"}


def test_school_url_allows_subdomains_only_over_https():
    assert is_school_url("https://ifes.kyungnam.ac.kr/x")
    assert is_school_url("https://www.kyungnam.ac.kr/ko/1")
    assert not is_school_url("http://www.kyungnam.ac.kr/ko/1")
    assert not is_school_url("https://kyungnam.ac.kr.evil.com/")
    assert not is_school_url("https://evilkyungnam.ac.kr/")


def test_phone_normalization():
    assert normalize_phone("(055)249-2027") == "055-249-2027"
    assert normalize_phone("055-249-2027") == "055-249-2027"


def test_unit_blocks_skip_staff_rows():
    lines = [
        "학사관리팀",
        "학사 업무 설명",
        "위치",
        ": 본관 1층",
        "전화",
        ": (055)249-2027(교육과정)",
        "김○○",
        "교무처 교무부 학사관리팀",
        "055-249-9999",
    ]
    [c] = extract_unit_blocks(lines, UNITS, LOC, "https://www.kyungnam.ac.kr/ko/6359/subview.do")
    assert (c.name, c.raw_location, c.phone) == ("학사관리팀", "본관 1층", "055-249-2027")
    assert "9999" not in (c.evidence_text or "")


def test_pairs_both_directions_and_multiple_names():
    lines = [
        "한마관 5층 : 장학복지팀",
        "시설관리팀, 구매관재팀 : 국제어학관 2층",
        "총장님 인사말",  # 콜론 없음
        "캠퍼스 : 경남 창원시",  # 위치 패턴 아님
    ]
    got = {c.name: c.raw_location for c in extract_pairs(lines, UNITS, LOC, "u")}
    assert got == {
        "장학복지팀": "한마관 5층",
        "시설관리팀": "국제어학관 2층",
        "구매관재팀": "국제어학관 2층",
    }


def test_tour_names_skip_non_buildings():
    html = "".join(
        f"<a onclick=\"jf_sel('ko','{i}')\">{n}</a>"
        for i, n in enumerate(["전경", "본관", "정문게시판"])
    )
    assert parse_tour(html) == ["본관"]

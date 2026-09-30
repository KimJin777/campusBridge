"""경남대 고유 용어 사전(교수님 #767)."""

from backend.app.glossary import hints, merge_terms, seed_terms


def test_seed_terms_include_campus_places():
    names = {t["term"] for t in seed_terms()}
    assert {"너른마당", "월영지"} <= names


def test_hints_match_term_or_alias_ignoring_spaces():
    terms = seed_terms()
    assert hints("너른 마당에서 버스 타요?", terms)[0].startswith("너른마당:")
    assert hints("월영지 어디야", terms)[0].startswith("월영지:")
    assert hints("휴학 신청 방법", terms) == []


def test_admin_rows_override_and_delete_seed():
    seeds = [{"term": "월영지", "aliases": [], "meaning": "연못", "source": "기본"}]
    rows = [
        {"term": "월영지", "status": "deleted"},
        {"term": "한마", "aliases": ["HANMA"], "meaning": "경남대 상징", "status": "active"},
    ]
    merged = merge_terms(seeds, rows)
    assert [t["term"] for t in merged] == ["한마"] and merged[0]["source"] == "관리자"
    assert hints("hanma 뜻", merged) == ["한마: 경남대 상징"]


def test_faq_follows_month():
    """수강신청 무렵엔 수강신청, 방학 직전엔 계절학기(교수님 #769). 통학버스는 항상(#768)."""
    from datetime import date

    from backend.app.faq import ALWAYS, monthly

    feb, jun, oct_ = (
        monthly(date(2027, 2, 10)),
        monthly(date(2027, 6, 1)),
        monthly(date(2026, 10, 1)),
    )
    assert any("수강신청" in q for q in feb["items"]) and feb["groups"][0]["hot"]
    assert any("계절학기" in q for q in jun["items"])
    assert all(ALWAYS in m["items"] and len(m["items"]) <= 6 for m in (feb, jun, oct_))
    assert {g["label"] for g in oct_["groups"]} >= {"수강신청", "계절학기", "학교생활"}



def test_faq_config_validate_and_view():
    """관리자 편집 목록 검사(교수님 #781): 겹침·없는 번호 정리, 월 목록 순서대로 칩."""
    import pytest

    from backend.app.faq import default_config, validate_config, view

    cfg = default_config()
    assert all(len(cfg["months"][str(m)]) <= 6 for m in range(1, 13))
    edited = {
        "questions": [
            {"id": "a", "text": "통학버스 시간표", "category": "학교생활"},
            {"id": "b", "text": "수강신청 기간", "category": "수강신청"},
        ],
        "months": {"10": ["b", "a", "b", "zz"]},
    }
    ok = validate_config(edited)
    assert ok["months"]["10"] == ["b", "a"] and ok["months"]["1"] == []
    v = view(ok, 10)
    assert v["items"] == ["수강신청 기간", "통학버스 시간표"] and v["groups"][0]["hot"]
    dup = {"questions": [{"id": "a", "text": "x"}, {"id": "b", "text": "x"}], "months": {}}
    with pytest.raises(ValueError):
        validate_config(dup)

# ruff: noqa: E501
from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.config import Settings
from backend.tools.directory import dept_lookup, find_campus_location


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def test_dept_lookup_reads_reviewed_directory(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "department_directory.csv",
        "dept_id,name,phone,duties,location_text,source_url,snapshot_at\n"
        "academic,학사지원팀,055-000-0000,학적,본관 1층,https://www.kyungnam.ac.kr/ko/,2026-09-29\n",
    )

    dept = dept_lookup("academic", path=path)

    assert dept is not None
    assert dept.name == "학사지원팀"
    assert dept.location_text == "본관 1층"
    assert dept_lookup("missing", path=path) is None


@pytest.mark.asyncio
async def test_location_uses_only_verified_rows_and_aliases(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "places.csv",
        "place_id,name,aliases,status,raw_location,source_url,snapshot_at\n"
        "main,한마관,학생회관|한마관,verified,정문 오른쪽,https://www.kyungnam.ac.kr/ko/8013/subview.do,2026-09-29\n"
        "secret,비공개실,,pending,지하,https://www.kyungnam.ac.kr/ko/8013/subview.do,2026-09-29\n",
    )
    settings = Settings()

    result = await find_campus_location("학생회관", settings=settings, path=path)
    hidden = await find_campus_location("비공개실", settings=settings, path=path)

    assert result.ok and result.items[0].id == "place:main"
    assert result.items[0].text == "한마관: 정문 오른쪽"
    assert hidden.ok and hidden.items == []


@pytest.mark.asyncio
async def test_location_does_not_guess_when_multiple_aliases_match(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "places.csv",
        "place_id,name,aliases,status,raw_location\n"
        "a,제1강의동,강의동,verified,동문\n"
        "b,제2강의동,강의동,verified,서문\n",
    )

    result = await find_campus_location("강의동", settings=Settings(), path=path)

    assert result.ok
    assert len(result.items) == 2
    assert result.message == "여러 장소가 있습니다"


@pytest.mark.asyncio
async def test_building_lists_units_inside_not_itself(tmp_path: Path) -> None:
    url = "https://www.kyungnam.ac.kr/ko/8013/subview.do"
    path = _write(
        tmp_path / "places.csv",
        "place_id,name,kind,parent_place_id,status,raw_location,source_url,snapshot_at\n"
        f"hanma,한마관,building,,verified,한마관,{url},2026-09-29\n"
        f"acad,학사관리팀,unit,,verified,한마관 2층,{url},2026-09-29\n"
        f"lib,중앙도서관,building,,verified,,{url},2026-09-29\n"
        f"cafe,학생식당,facility,hanma,verified,,{url},2026-09-29\n"
        f"hidden,비공개실,unit,hanma,pending,,{url},2026-09-29\n",
    )
    result = await find_campus_location("한마관", settings=Settings(), path=path)

    assert result.ok
    texts = [item.text for item in result.items]
    assert texts[0] == "한마관"  # "한마관: 한마관" 자기 반복 없음
    assert texts[1] == "한마관에 있는 부서·시설: 학사관리팀(한마관 2층), 학생식당(한마관)"
    assert "비공개실" not in texts[1]


def test_place_lookup_failure_is_not_cached_as_empty(monkeypatch):
    """Firestore 조회 실패를 빈 목록으로 5분간 캐시하지 않는다(교수님 2026-09-30 위치 0건)."""
    import google.cloud.firestore as fs

    from backend.app.config import Settings
    from backend.tools import directory

    calls = {"n": 0}

    class Snap:
        id = "u1"

        def to_dict(self):
            return {"name": "학사관리팀", "status": "verified", "raw_location": "본관 1층"}

    class Query:
        def where(self, *a, **k):
            return self

        def stream(self):
            return [Snap()]

    class Client:
        def __init__(self, *a, **k):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise RuntimeError("first connection failed")

        def collection(self, _):
            return Query()

    monkeypatch.setattr(fs, "Client", Client)
    monkeypatch.setattr(directory.time, "sleep", lambda s: None)
    directory._places_cache.update(at=float("-inf"), rows=[])
    settings = Settings(gcp_project_id="p")
    assert directory._firestore_place_rows(settings) == []  # 두 번 모두 실패
    rows = directory._firestore_place_rows(settings)  # 다음 호출에서 바로 다시 조회
    assert rows and rows[0]["name"] == "학사관리팀"


def test_fresh_instance_queries_immediately(monkeypatch):
    """새 인스턴스(time.monotonic이 작음)도 첫 호출에 바로 DB를 읽는다 — 배포 직후 위치 0건 원인."""
    import google.cloud.firestore as fs

    from backend.app.config import Settings
    from backend.tools import directory

    class Snap:
        id = "u1"

        def to_dict(self):
            return {"name": "학사관리팀", "status": "verified", "raw_location": "본관 1층"}

    class Query:
        def where(self, *a, **k):
            return self

        def stream(self):
            return [Snap()]

    class Client:
        def __init__(self, *a, **k):
            pass

        def collection(self, _):
            return Query()

    monkeypatch.setattr(fs, "Client", Client)
    monkeypatch.setattr(directory.time, "monotonic", lambda: 12.0)  # 켜진 지 12초
    directory._places_cache.update(at=float("-inf"), rows=[])
    assert directory._firestore_place_rows(Settings(gcp_project_id="p"))[0]["name"] == "학사관리팀"


def test_phone_question_gets_phonebook_evidence(monkeypatch) -> None:
    """'역사학과 전화번호' → 전화번호부 근거(조교 번호 여럿), 부서 카드도 ID로 찾음(교수님 2026-10-01)."""
    import asyncio

    from backend.tools import directory as D

    rows = {
        "역사학과": {"id": "u-hist", "name": "역사학과", "phone": "055-249-2147", "phones": ["055-249-2147"], "fax": "0505-999-2138"},
        "사회복지학과": {"id": "u-sw", "name": "사회복지학과", "phone": "055-249-2173", "phones": ["055-249-2173", "055-249-2118"]},
    }
    monkeypatch.setattr(D, "_directory_rows", lambda s: rows)
    monkeypatch.setattr(D, "_verified_rows", lambda s, p: [])
    s = Settings(gcp_project_id="p")
    res = asyncio.run(D.find_campus_location("사회복지학과 사무실 전화번호", settings=s))
    assert res.ok and res.items[0].id == "dept:u-sw" and res.items[0].kind == "department"
    assert "055-249-2173, 055-249-2118" in res.items[0].text
    dept = D.dept_lookup("u-hist")
    assert dept is not None and dept.phones == ["055-249-2147"]


def test_contact_question_plans_directory_lookup() -> None:
    from backend.agent.needs import plan_calls

    calls = plan_calls(["procedure_and_contact"], search_query="역사학과 사무실 전화번호", original_query="역사학과 사무실 전화번호 알려줘")
    assert any(c["name"] == "find_campus_location" for c in calls)
    assert not any(c["name"] == "find_campus_tips" for c in calls)
    plain = plan_calls(["procedure_and_contact"], search_query="휴학 신청 방법")
    assert not any(c["name"] == "find_campus_location" for c in plain)

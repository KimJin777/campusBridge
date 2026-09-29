from __future__ import annotations

import asyncio

import pytest

from backend.admin.places import PlaceCreate, PlacePatch, normalize_place_changes
from backend.app.config import Settings
from backend.domain import AppError
from backend.tools import directory

S = Settings()
URL = "https://www.kyungnam.ac.kr/ko/4251/subview.do"
VERIFIED = {
    "name": "학사관리팀",
    "kind": "unit",
    "raw_location": "본관 1층",
    "source_url": URL,
    "snapshot_at": "2026-09-29",
    "status": "verified",
}


def test_new_place_always_starts_pending():
    out = normalize_place_changes({"name": "학사관리팀", "kind": "unit"}, None, S)
    assert out["status"] == "pending"
    with pytest.raises(AppError, match="pending으로 등록"):
        normalize_place_changes({"name": "x", "status": "verified"}, None, S)


def test_verify_requires_source_and_date_and_location():
    pending = {"name": "학사관리팀", "kind": "unit", "status": "pending"}
    with pytest.raises(AppError, match="원문 주소와 확인일"):
        normalize_place_changes({"status": "verified"}, pending, S)
    with pytest.raises(AppError, match="위치"):
        normalize_place_changes(
            {"status": "verified", "source_url": URL, "snapshot_at": "2026-09-29"}, pending, S
        )
    out = normalize_place_changes(
        {
            "status": "verified",
            "source_url": URL,
            "snapshot_at": "2026-09-29",
            "raw_location": "본관 1층",
        },
        pending,
        S,
    )
    assert out["status"] == "verified"


def test_non_school_url_rejected_and_location_edit_resets_review():
    with pytest.raises(AppError, match="https"):
        normalize_place_changes({"source_url": "https://evil.example/x"}, VERIFIED, S)
    out = normalize_place_changes({"raw_location": "본관 2층"}, VERIFIED, S)
    assert out["status"] == "pending"  # 검수된 위치를 고치면 재검수
    note_only = normalize_place_changes({"note": "전화 확인"}, VERIFIED, S)
    assert "status" not in note_only


def test_request_models_validate_ids_and_empty_patch():
    with pytest.raises(ValueError):
        PlaceCreate(
            place_id="Bad Id!", name="x", kind="unit", reason="r", request_id="req-00000001"
        )
    with pytest.raises(ValueError):
        PlacePatch(reason="r", request_id="req-00000001")
    ok = PlaceCreate(
        place_id="Acad_Office",
        name="학사관리팀",
        kind="unit",
        reason="r",
        request_id="req-00000001",
        aliases=[" 학사팀 ", "학사팀", ""],
    )
    assert ok.place_id == "acad_office" and ok.aliases == ["학사팀"]


def test_lookup_prefers_firestore_verified_rows(monkeypatch, tmp_path):
    csv = tmp_path / "places.csv"
    csv.write_text(
        "place_id,name,aliases,raw_location,status\n"
        "acad,학사관리팀,학사팀,본관 1층(옛 위치),verified\n"
        "lib,중앙도서관,,도서관동,pending\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(directory, "DATA_DIR", tmp_path)
    monkeypatch.setattr(
        directory,
        "_firestore_place_rows",
        lambda s: [
            {
                "place_id": "acad",
                "name": "학사관리팀",
                "aliases": "학사팀",
                "raw_location": "본관 2층",
                "status": "verified",
            },
        ],
    )
    res = asyncio.run(directory.find_campus_location("학사팀", settings=S))
    assert res.ok and res.items[0].text == "학사관리팀: 본관 2층"
    none = asyncio.run(directory.find_campus_location("중앙도서관", settings=S))
    assert none.items == []  # pending은 노출 금지


def test_subdomain_source_url_accepted_and_dept_lookup_uses_verified_units(monkeypatch):
    out = normalize_place_changes(
        {"source_url": "https://ifes.kyungnam.ac.kr/about"}, {"status": "pending"}, S
    )
    assert out["source_url"].startswith("https://ifes.")
    monkeypatch.setattr(
        directory,
        "_verified_rows",
        lambda s, p: [
            {
                "place_id": "u-1",
                "kind": "unit",
                "name": "학사관리팀",
                "phone": "055-249-2027",
                "raw_location": "본관 1층",
                "source_url": URL,
                "status": "verified",
            },
        ],
    )
    d = directory.dept_lookup("학사관리팀", path=None)
    assert d and (d.name, d.phone, d.location_text) == ("학사관리팀", "055-249-2027", "본관 1층")
    assert directory.dept_lookup("없는팀") is None


def test_unit_with_phone_only_can_be_verified():
    unit = {
        "name": "연구윤리센터",
        "kind": "unit",
        "phone": "055-249-2121",
        "status": "pending",
        "source_url": URL,
        "snapshot_at": "2026-09-29",
    }
    assert normalize_place_changes({"status": "verified"}, unit, S)["status"] == "verified"

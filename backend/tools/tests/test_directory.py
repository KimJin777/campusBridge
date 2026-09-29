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

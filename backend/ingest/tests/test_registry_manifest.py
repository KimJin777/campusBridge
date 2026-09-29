from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from backend.ingest.download import HWP_OLE_MAGIC, download_rules
from backend.ingest.manifest import (
    ManifestEntry,
    load_manifest,
    save_manifest,
    source_changed,
)
from backend.ingest.registry import RuleRecord, parse_part_links, parse_rule_page

REGISTRY_FIXTURE = """
<html><body>
  <a href="index.cfm?kd=part2&nd=two">제2편 헌장 및 학칙</a>
  <a href="index.cfm?kd=part3&nd=three">제3편 학사행정</a>
</body></html>
"""

PART_FIXTURE = """
<html><body>
  <h2 class="maintitl">제2편 헌장 및 학칙</h2>
  <table summary="연번, 규정명, 소관부서, 제정일자, 개정일자, 개정차수, 규정이력">
    <tbody>
      <tr>
        <td>1</td><td>경남대학교 학칙</td><td>학사지원팀</td>
        <td>1971.01.01</td><td>2026.03.01</td><td>66차</td>
        <td><a href="/rule/rdata/00000009/reg101_2365.hwp">History</a></td>
      </tr>
      <tr>
        <td>2</td><td>성적처리 규정</td><td>학사지원팀</td>
        <td>2000.01.01</td><td></td><td>제정</td>
        <td><a href="/rule/rdata/00000002/reg202_1044.hwp">원문</a></td>
      </tr>
    </tbody>
  </table>
</body></html>
"""


def _entry(**overrides: object) -> ManifestEntry:
    values: dict[str, object] = {
        "source_url": "https://yz.kyungnam.ac.kr/rule/rdata/x/reg101_1.hwp",
        "file_version": "1",
        "fetched_at": "2026-09-29T00:00:00Z",
        "etag": None,
        "last_modified": None,
        "content_hash": "sha256:abc",
    }
    values.update(overrides)
    return ManifestEntry(**values)  # type: ignore[arg-type]


def test_parse_part_links_is_allowlisted() -> None:
    links = parse_part_links(REGISTRY_FIXTURE)
    assert links[2].startswith("https://yz.kyungnam.ac.kr/rule/index.cfm?")
    assert links[3].endswith("nd=three")


def test_parse_rule_page_extracts_current_hwp_metadata() -> None:
    records = parse_rule_page(
        PART_FIXTURE,
        part=2,
        page_url="https://yz.kyungnam.ac.kr/rule/index.cfm?part=2",
    )
    assert len(records) == 2
    assert records[0].rule_no == "101"
    assert records[0].file_version == "2365"
    assert records[0].revision_no == 66
    assert records[0].rule_level == "학칙"
    assert records[1].revision_no == 0
    assert records[1].revision_date is None


def test_parse_part_links_rejects_external_registry_link() -> None:
    fixture = '<a href="https://evil.example/rule/">제2편 학칙</a>'
    with pytest.raises(ValueError, match="outside"):
        parse_part_links(fixture)


def test_manifest_round_trip_is_stable_and_change_detection_uses_hash_and_version(
    tmp_path: Path,
) -> None:
    path = tmp_path / "processed" / "manifest.json"
    entries = {"101": _entry(status="validated", article_count=42)}
    save_manifest(path, entries)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["101"]["license_basis"] == "공개 규정"
    loaded = load_manifest(path)
    assert loaded["101"].article_count == 42
    assert source_changed(loaded["101"], file_version="1", content_hash="sha256:abc") is False
    assert source_changed(loaded["101"], file_version="2", content_hash="sha256:abc") is True
    assert source_changed(loaded["101"], file_version="1", content_hash="sha256:def") is True


def test_invalid_manifest_root_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="root"):
        load_manifest(path)


@pytest.mark.asyncio
async def test_download_writes_hwp_and_manifest_atomically(tmp_path: Path) -> None:
    content = HWP_OLE_MAGIC + bytes(1_024)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["user-agent"].startswith("CampusBridge/")
        return httpx.Response(
            200,
            content=content,
            headers={"content-type": "application/octet-stream", "etag": '"abc"'},
        )

    record = RuleRecord(
        rule_no="101",
        rule_name="경남대학교 학칙",
        part=2,
        part_name="제2편 헌장 및 학칙",
        revision_no=1,
        revision_date="2026.01.01",
        enacted_date="1971.01.01",
        department="학사지원팀",
        hwp_url="https://yz.kyungnam.ac.kr/rule/rdata/00000001/reg101_2.hwp",
        file_version="2",
        rule_level="학칙",
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"User-Agent": "CampusBridge/0.1 (test)"},
    ) as client:
        entries = await download_rules(
            [record],
            output_dir=tmp_path / "raw",
            manifest_path=tmp_path / "processed" / "manifest.json",
            min_interval_seconds=0,
            client=client,
        )

    assert (tmp_path / "raw" / "101_2.hwp").read_bytes() == content
    assert entries["101"].status == "fetched"
    assert entries["101"].etag == '"abc"'
    assert entries["101"].content_hash.startswith("sha256:")


@pytest.mark.asyncio
async def test_download_rejects_html_body(tmp_path: Path) -> None:
    record = RuleRecord(
        rule_no="101",
        rule_name="학칙",
        part=2,
        part_name="제2편",
        revision_no=1,
        revision_date=None,
        enacted_date=None,
        department=None,
        hwp_url="https://yz.kyungnam.ac.kr/rule/rdata/x/reg101_2.hwp",
        file_version="2",
        rule_level="학칙",
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text="<html>blocked</html>")
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ValueError, match="small|HTML"):
            await download_rules(
                [record],
                output_dir=tmp_path / "raw",
                manifest_path=tmp_path / "manifest.json",
                min_interval_seconds=0,
                client=client,
            )

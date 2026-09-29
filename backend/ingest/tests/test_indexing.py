from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from backend.ingest.indexing import _iso_date, _write_jsonl, build_structured_document
from backend.ingest.manifest import ManifestEntry
from backend.ingest.registry import RuleRecord
from backend.ingest.rules import Article, ParseMode


def _record() -> RuleRecord:
    return RuleRecord(
        rule_no="29",
        rule_name="경남대학교 학칙",
        part=2,
        part_name="제2편 헌장 및 학칙",
        revision_no=131,
        revision_date="2026.08.12",
        enacted_date="1975.09.01",
        department="정책기획팀",
        hwp_url="https://yz.kyungnam.ac.kr/rule/rdata/x/reg29_2363.hwp",
        file_version="2363",
        rule_level="학칙",
    )


def _manifest() -> ManifestEntry:
    return ManifestEntry(
        source_url="https://yz.kyungnam.ac.kr/rule/rdata/x/reg29_2363.hwp",
        file_version="2363",
        fetched_at="2026-09-29T00:00:00Z",
        etag=None,
        last_modified=None,
        content_hash="sha256:abc",
    )


def test_structured_document_preserves_search_and_citation_fields() -> None:
    article = Article(
        rule_no="29",
        number=38,
        title="휴학",
        body_lines=["① 휴학원을 제출한다.", "[표 — 원문 참조]"],
    )

    document = build_structured_document(
        article,
        _record(),
        _manifest(),
        dept_id="policy_planning",
    )

    data = document["structData"]
    assert document["id"] == "29_main_38"
    assert data["body"] == article.body
    assert data["source_url"] == _record().hwp_url
    assert data["revision_date"] == "2026-08-12"
    assert data["has_table"] is True
    assert data["access"] == "public"
    assert data["dept_id"] == "policy_planning"


def test_structured_document_keeps_addenda_namespace_and_date() -> None:
    article = Article(
        rule_no="29",
        number=0,
        mode=ParseMode.ADDENDA,
        addenda_seq=116,
        addenda_date=dt.date(2026, 8, 12),
        body_lines=["이 개정 학칙은 2026년 9월 1일부터 시행한다."],
    )

    data = build_structured_document(article, _record(), _manifest())["structData"]

    assert data["article_id"] == "29_add_s116_0"
    assert data["kind"] == "addenda"
    assert data["addenda_date"] == "2026-08-12"
    assert "dept_id" not in data
    assert "article_title" not in data
    assert "article_branch" not in data


def test_jsonl_writer_is_utf8_one_document_per_line(tmp_path: Path) -> None:
    output = tmp_path / "articles.jsonl"
    documents = [
        {"id": "a", "structData": {"body": "휴학"}},
        {"id": "b", "structData": {"body": "복학"}},
    ]

    _write_jsonl(output, documents)

    lines = output.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == documents


def test_registry_date_rejects_unknown_format() -> None:
    assert _iso_date("2026.8.12") == "2026-08-12"
    with pytest.raises(ValueError, match="unsupported"):
        _iso_date("2026년 8월 12일")

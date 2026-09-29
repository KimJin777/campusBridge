from __future__ import annotations

import json
from pathlib import Path

from backend.ingest.search_spike import _write_overwrite_probe, build_search_schema


def test_search_schema_marks_required_fields() -> None:
    schema = build_search_schema()
    properties = schema["properties"]

    assert schema["dynamic"] == "false"
    assert properties["body"]["keyPropertyMapping"] == "description"
    assert properties["body"]["retrievable"] is True
    assert properties["article_title"]["keyPropertyMapping"] == "title"
    assert properties["source_url"]["keyPropertyMapping"] == "uri"
    assert properties["access"]["indexable"] is True
    assert properties["kind"]["indexable"] is True


def test_overwrite_probe_changes_only_target_document(tmp_path: Path) -> None:
    source = tmp_path / "source.jsonl"
    output = tmp_path / "probe.jsonl"
    documents = [
        {"id": "a", "structData": {"body": "A"}},
        {"id": "b", "structData": {"body": "B"}},
    ]
    source.write_text(
        "".join(json.dumps(item) + "\n" for item in documents),
        encoding="utf-8",
    )

    _write_overwrite_probe(source, output, "a")

    result = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert result[0]["structData"]["preflight_revision"] == "overwrite-v2"
    assert "preflight_revision" not in result[1]["structData"]

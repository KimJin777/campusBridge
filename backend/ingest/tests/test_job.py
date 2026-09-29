from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from backend.ingest.job import JobDeps, process_document
from backend.ingest.uploads import ExtractionError, build_upload_documents, chunk_text, extract_text

TEXT = (
    "휴학 신청 안내\n\n"
    + "휴학 신청은 학생정보시스템에서 할 수 있으며 학사관리팀에서 최종 승인합니다. " * 3
)


class FakeBlobs:
    def __init__(self, files: dict[str, bytes] | None = None):
        self.files = dict(files or {})

    def read(self, name: str) -> bytes | None:
        return self.files.get(name)

    def write(self, name: str, data: bytes, content_type: str) -> None:
        self.files[name] = data

    def delete_prefix(self, prefix: str) -> int:
        gone = [k for k in self.files if k.startswith(prefix)]
        for k in gone:
            del self.files[k]
        return len(gone)


class FakeDocs:
    def __init__(self, docs: dict[str, dict[str, Any]]):
        self.docs = docs

    def get(self, collection: str, doc_id: str):
        return self.docs.get(doc_id)

    def merge(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        self.docs.setdefault(doc_id, {}).update(data)


class FakeIndex:
    def __init__(self, errors: list[str] | None = None):
        self.imported: list[str] = []
        self.deleted: list[str] = []
        self.errors = errors or []

    def import_jsonl(self, local_path: Path, object_prefix: str) -> list[str]:
        self.imported += [
            json.loads(line)["id"] for line in local_path.read_text("utf-8").splitlines()
        ]
        return self.errors

    def delete(self, vertex_ids: list[str]) -> int:
        self.deleted += vertex_ids
        return len(vertex_ids)


def deps(doc: dict[str, Any], files: dict[str, bytes], index: FakeIndex | None = None):
    return JobDeps(
        blobs=FakeBlobs(files), docs=FakeDocs({"doc-1": doc}), index=index or FakeIndex()
    )


def test_extract_txt_docx_and_reject_empty():
    assert extract_text(TEXT.encode(), "txt").startswith("휴학 신청 안내")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        body = "".join(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in TEXT.split("\n\n"))
        z.writestr("word/document.xml", f"<w:document><w:body>{body}</w:body></w:document>")
    assert "학사관리팀" in extract_text(buf.getvalue(), "docx")
    with pytest.raises(ExtractionError):
        extract_text("짧음".encode(), "txt")


def test_chunking_respects_limit_and_order():
    text = "\n\n".join(f"문단{i} " + "가" * 400 for i in range(10))
    chunks = chunk_text(text, max_chars=1_000)
    assert all(len(c) <= 1_000 for c in chunks) and chunks[0].startswith("문단0")
    assert "".join(chunks).count("문단") == 10
    assert all(len(c) <= 1_000 for c in chunk_text("나" * 2_500, max_chars=1_000))


def test_upload_documents_ids_and_fields():
    docs = build_upload_documents(
        "doc-1", {"title": "휴학 안내", "department": "학사관리팀"}, ["a", "b"]
    )
    assert [d["id"] for d in docs] == ["upload-doc-1-1", "upload-doc-1-2"]
    sd = docs[0]["structData"]
    assert (
        sd["article_id"] == "doc:doc-1:1"
        and sd["parent_document_id"] == "doc-1"
        and sd["source_kind"] == "guide"
        and sd["access"] == "public"
    )


def test_preview_then_publish_then_purge():
    doc = {
        "status": "staging",
        "format": "txt",
        "title": "휴학 안내",
        "gcs_staging_path": "documents/staging/doc-1/original.txt",
    }
    d = deps(doc, {"documents/staging/doc-1/original.txt": TEXT.encode()})
    out = process_document("preview", "doc-1", d)
    assert out["preview_status"] == "ready" and out["preview_chunk_count"] >= 1
    assert "documents/staging/doc-1/chunks.json" in d.blobs.files

    d.docs.docs["doc-1"]["status"] = "publishing"
    out = process_document("publish", "doc-1", d)
    assert out["status"] == "published" and d.index.imported == out["indexed_ids"]

    out = process_document("purge", "doc-1", d)
    assert out["status"] == "purged" and d.index.deleted == ["upload-doc-1-1"]
    assert not any(k.startswith("documents/staging/doc-1/") for k in d.blobs.files)


def test_failures_are_structured():
    doc = {"status": "staging", "format": "pdf", "gcs_staging_path": "x.pdf"}
    d = deps(doc, {})
    assert process_document("preview", "doc-1", d)["preview_error_code"] == "SOURCE_MISSING"
    d = deps({**doc, "status": "staging"}, {"x.pdf": b"%PDF-1.4 broken"})
    assert process_document("preview", "doc-1", d)["preview_error_code"] == "EXTRACTION_FAILED"
    d = deps({"status": "staging"}, {})
    assert process_document("publish", "doc-1", d)["job_error_code"] == "INVALID_STATE"
    d = deps(
        {"status": "publishing", "title": "t"},
        {"documents/staging/doc-1/chunks.json": b'["body"]'},
        FakeIndex(errors=["bad"]),
    )
    assert process_document("publish", "doc-1", d)["job_error_code"] == "IMPORT_FAILED"

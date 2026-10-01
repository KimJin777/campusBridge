"""교내 문서 업로드 칸 자동 채움(교수님 2026-10-01): 근거 확인·잠금 검사."""

from __future__ import annotations

from backend.admin.doc_extract import (
    DocFields,
    Extracted,
    clean_fields,
    get_extraction_records,
    get_field_extractor,
    locked_mismatch,
)
from backend.admin.tests.test_router import FakeDocumentStorage, FakeStore, _client

TEXT = (
    "경남대학교 학생지원처\n문서번호 학생지원처-1234 (2026. 9. 1.)\n"
    "제목 2026학년도 2학기 국가근로장학생 추가 모집 안내\n"
    "모집 기간은 9월 15일까지이며 선발 결과는 개별 통보합니다. 문의는 학생지원처로 해 주세요."
)


def _raw(**over: Extracted) -> DocFields:
    base = {
        "title": Extracted(
            value="2026학년도 2학기 국가근로장학생 추가 모집 안내",
            evidence="제목 2026학년도 2학기 국가근로장학생",
        ),
        "department": Extracted(value="학생지원처", evidence="경남대학교 학생지원처"),
        "doc_date": Extracted(value="2026-09-01", evidence="학생지원처-1234 (2026. 9. 1.)"),
        "effective_from": Extracted(),
        "expires_at_doc": Extracted(),
        "source": Extracted(value="학생지원처-1234", evidence="문서번호 학생지원처-1234"),
    }
    base.update(over)
    return DocFields(**base)


def test_clean_fields_keeps_grounded_values_and_fills_effective_from() -> None:
    out = clean_fields(_raw(), TEXT)

    assert out["source"]["value"] == "학생지원처-1234"
    assert out["doc_date"]["value"] == "2026-09-01"
    assert out["effective_from"]["value"] == "2026-09-01"  # 시행일 없음 → 문서일
    assert "expires_at_doc" not in out


def test_clean_fields_drops_values_without_evidence_in_text() -> None:
    raw = _raw(
        expires_at_doc=Extracted(value="2027-02-28", evidence="유효기간 2027. 2. 28."),
        department=Extracted(value="학생처", evidence=None),
    )

    out = clean_fields(raw, TEXT)

    assert "expires_at_doc" not in out  # 근거 문장이 본문에 없음
    assert "department" not in out  # 근거 없음


def test_clean_fields_drops_bad_dates_and_skips_check_without_text() -> None:
    raw = _raw(doc_date=Extracted(value="2026.9.1", evidence="(2026. 9. 1.)"))

    out = clean_fields(raw, None)  # 스캔 PDF: 본문이 없으면 근거 확인 생략

    assert "doc_date" not in out
    assert out["title"]["value"].startswith("2026학년도")


def test_locked_mismatch_detects_changed_file_or_value() -> None:
    record = {"sha256": "abc", "fields": {"title": "안내", "doc_date": "2026-09-01"}}

    assert locked_mismatch(record, "abc", {"title": "안내", "doc_date": "2026-09-01"}) is None
    assert "파일" in locked_mismatch(record, "zzz", {"title": "안내", "doc_date": "2026-09-01"})
    assert "수정할 수 없습니다" in locked_mismatch(
        record, "abc", {"title": "고침", "doc_date": "2026-09-01"}
    )


class FakeRecords:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    async def save(self, extraction_id, data):
        self.rows[extraction_id] = data

    async def get(self, extraction_id):
        return self.rows.get(extraction_id)


async def _fake_extractor(document, text):
    return _raw()


def _api(records: FakeRecords, storage: FakeDocumentStorage | None = None):
    client = _client(FakeStore(), document_storage=storage)
    client.app.dependency_overrides[get_field_extractor] = lambda: _fake_extractor
    client.app.dependency_overrides[get_extraction_records] = lambda: records
    return client


FORM = {
    "approval_basis": "공개 답변 인용 승인 2026-09-29",
    "reason": "신규 안내 반영",
    "request_id": "upload-12345",
    "expires_at_doc": "2026-12-31",
}


def test_extract_then_upload_with_locked_values() -> None:
    records = FakeRecords()
    storage = FakeDocumentStorage()
    client = _api(records, storage)
    file = ("notice.txt", TEXT.encode(), "text/plain")

    got = client.post("/api/admin/documents/extract", files={"file": file})
    assert got.status_code == 200
    body = got.json()
    values = {k: v["value"] for k, v in body["fields"].items()}
    assert values["department"] == "학생지원처"

    ok = client.post(
        "/api/admin/documents",
        data={**FORM, **values, "extraction_id": body["extraction_id"]},
        files={"file": file},
    )
    assert ok.status_code == 202
    assert len(storage.calls) == 1

    tampered = client.post(
        "/api/admin/documents",
        data={
            **FORM,
            **values,
            "title": "다른 제목",
            "request_id": "upload-67890",
            "extraction_id": body["extraction_id"],
        },
        files={"file": file},
    )
    assert tampered.status_code == 400
    assert len(storage.calls) == 1  # 저장하지 않음


def test_upload_rejects_unknown_extraction_id() -> None:
    client = _api(FakeRecords())
    r = client.post(
        "/api/admin/documents",
        data={
            **FORM,
            "title": "t",
            "department": "d",
            "doc_date": "2026-09-01",
            "effective_from": "2026-09-01",
            "source": "s",
            "extraction_id": "ext-none",
        },
        files={"file": ("a.txt", TEXT.encode(), "text/plain")},
    )
    assert r.status_code == 400

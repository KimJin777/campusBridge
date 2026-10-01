# ruff: noqa: E501 — 한국어 설명·프롬프트 문장은 줄바꿈하지 않는다
"""교내 문서 업로드 칸 자동 채움(교수님 2026-10-01).

파일을 고르면 문서에서 제목·소관부서·문서일·시행일·만료일·출처를 뽑아 업로드 칸을 채운다.
- 모델: 정확도 최고(settings.doc_extract_model). PDF는 원본을 그대로(스캔·표 포함), 그 밖은 본문 텍스트.
- 문서에 적힌 값만 채운다. 텍스트를 뽑을 수 있으면 근거 문장이 본문에 실제로 있는지 확인해 없으면 버린다.
- 채운 값은 화면에서 잠그고, 서버에도 기록(extraction_id)해 업로드 때 파일 해시·값이 같은지 검사한다.
  승인 근거·사유는 사람이 판단할 내용이라 채우지 않는다.
"""

from __future__ import annotations

import functools
import re
import uuid
from datetime import UTC, date, datetime
from typing import Any, Protocol

from pydantic import BaseModel, Field

from backend.admin.document_files import ValidatedDocument
from backend.app.clients import call_with_retry
from backend.app.config import Settings

# 업로드 폼 칸 이름 → 최대 길이(router.upload_document의 Form 제한과 같다). 날짜 칸은 None
FIELDS: dict[str, int | None] = {
    "title": 200,
    "department": 100,
    "doc_date": None,
    "effective_from": None,
    "expires_at_doc": None,
    "source": 300,
}
MAX_TEXT_CHARS = 60_000
COLLECTION = "document_extractions"


class Extracted(BaseModel):
    value: str | None = Field(None, description="문서에 적힌 값. 없으면 null")
    evidence: str | None = Field(None, description="값이 나온 문서 속 문장을 그대로(30자 안팎)")


class DocFields(BaseModel):
    title: Extracted = Field(description="문서 제목(공문이면 '제목' 줄의 내용)")
    department: Extracted = Field(
        description="소관부서: 문서를 낸 교내 부서명(기관명 '○○대학교'는 빼고)"
    )
    doc_date: Extracted = Field(description="문서일(시행·발송 일자) YYYY-MM-DD")
    effective_from: Extracted = Field(description="내용의 시행일·적용 시작일 YYYY-MM-DD")
    expires_at_doc: Extracted = Field(description="만료일·적용 종료일·유효기간 끝 YYYY-MM-DD")
    source: Extracted = Field(description="출처: 문서번호·공문 번호(예: 학생지원처-1234)")


PROMPT = """너는 대학 교내 문서의 서지 정보를 옮겨 적는 사람이다.
첨부한 문서에서 아래 칸의 값을 찾아 JSON으로 답한다.
규칙:
- 문서에 실제로 적힌 값만 쓴다. 추측·계산·일반 상식으로 채우지 않는다. 없으면 value와 evidence 모두 null.
- evidence에는 값이 나온 문장을 문서에서 그대로 복사한다(고쳐 쓰지 않는다).
- 날짜는 YYYY-MM-DD로 바꾼다(예: 2026. 9. 1. → 2026-09-01). 연도가 없으면 null.
- 소관부서는 문서를 낸 부서다(발신 명의·기안 부서). 대학 이름은 빼고 부서명만 쓴다.
- 출처는 문서번호(생산등록번호·시행 번호)를 쓴다. 없으면 null."""


class ExtractionRecords(Protocol):
    async def save(self, extraction_id: str, data: dict[str, Any]) -> None: ...

    async def get(self, extraction_id: str) -> dict[str, Any] | None: ...


class FirestoreExtractionRecords:
    """Firestore 연결은 실제로 읽고 쓸 때 만든다(자동 채움 없이 올리는 업로드는 연결하지 않음)."""

    def __init__(self, db: Any = None):
        self._db = db

    @property
    def db(self) -> Any:
        if self._db is None:
            self._db = _firestore_db()
        return self._db

    async def save(self, extraction_id: str, data: dict[str, Any]) -> None:
        await self.db.collection(COLLECTION).document(extraction_id).set(data)

    async def get(self, extraction_id: str) -> dict[str, Any] | None:
        snap = await self.db.collection(COLLECTION).document(extraction_id).get()
        return snap.to_dict() if snap.exists else None


@functools.cache
def _firestore_db() -> Any:
    from google.cloud import firestore

    from backend.app.config import get_settings

    s = get_settings()
    return firestore.AsyncClient(project=s.gcp_project_id or None, database=s.firestore_db)


def get_extraction_records() -> ExtractionRecords:
    return FirestoreExtractionRecords()


class FieldExtractor(Protocol):
    async def __call__(self, document: ValidatedDocument, text: str | None) -> DocFields: ...


class GeminiFieldExtractor:
    """정확도 최고 모델 1회 호출(재시도 1회). PDF는 원본 바이트, 그 밖은 본문 텍스트."""

    def __init__(self, settings: Settings):
        self.s = settings

    async def __call__(self, document: ValidatedDocument, text: str | None) -> DocFields:
        from google import genai
        from google.genai import types

        client = genai.Client(
            vertexai=True, project=self.s.gcp_project_id or None, location=self.s.gemini_location
        )
        if document.format == "pdf":
            body: Any = types.Part.from_bytes(data=document.content, mime_type="application/pdf")
        else:
            body = f"[문서 본문]\n{(text or '')[:MAX_TEXT_CHARS]}"
        config = types.GenerateContentConfig(
            system_instruction=PROMPT,
            temperature=0,
            response_mime_type="application/json",
            response_schema=DocFields,
        )

        async def call() -> DocFields:
            resp = await client.aio.models.generate_content(
                model=self.s.doc_extract_model, contents=[body], config=config
            )
            if isinstance(resp.parsed, DocFields):
                return resp.parsed
            return DocFields.model_validate_json(resp.text or "")

        return await call_with_retry(
            call, timeout=120.0, attempts=2, target=f"gemini:doc_extract:{self.s.doc_extract_model}"
        )


def get_field_extractor() -> FieldExtractor:
    from backend.app.config import get_settings

    return GeminiFieldExtractor(get_settings())


def document_text(document: ValidatedDocument) -> str | None:
    """근거 확인·모델 입력용 본문. 뽑지 못하면(스캔 PDF 등) None."""
    from backend.ingest.uploads import extract_text

    try:
        return extract_text(document.content, document.format)
    except Exception:  # noqa: BLE001 — 본문이 없으면(스캔 PDF·변환기 없음) 근거 확인만 생략한다
        return None


_SPACE = re.compile(r"\s+")


def _squash(s: str) -> str:
    return _SPACE.sub("", s)


def clean_fields(raw: DocFields, text: str | None) -> dict[str, dict[str, str]]:
    """모델 답을 검사해 채울 칸만 남긴다: 형식이 맞고, 본문이 있으면 근거가 본문에 실제로 있어야 한다."""
    haystack = _squash(text) if text else None
    out: dict[str, dict[str, str]] = {}
    for name, limit in FIELDS.items():
        got: Extracted = getattr(raw, name)
        value = (got.value or "").strip()
        evidence = (got.evidence or "").strip()
        if not value:
            continue
        if limit is None:
            try:
                value = date.fromisoformat(value).isoformat()
            except ValueError:
                continue
        elif len(value) > limit:
            continue
        if haystack is not None and (not evidence or _squash(evidence) not in haystack):
            continue  # 근거를 본문에서 찾지 못함 → 지어낸 값일 수 있어 채우지 않는다
        out[name] = {"value": value, "evidence": evidence[:300]}
    # 시행일이 따로 없으면 문서일로(교수님 승인 안 2026-10-01)
    if "effective_from" not in out and "doc_date" in out:
        out["effective_from"] = {
            "value": out["doc_date"]["value"],
            "evidence": "시행일이 따로 없어 문서일과 같게 채움",
        }
    return out


async def extract_document_fields(
    document: ValidatedDocument,
    *,
    extractor: FieldExtractor,
    records: ExtractionRecords,
    actor_email: str,
    model: str,
) -> dict[str, Any]:
    text = document_text(document)
    raw = await extractor(document, text)
    fields = clean_fields(raw, text)
    extraction_id = f"ext-{uuid.uuid4().hex}"
    await records.save(
        extraction_id,
        {
            "sha256": document.sha256,
            "format": document.format,
            "fields": {k: v["value"] for k, v in fields.items()},
            "actor": actor_email,
            "model": model,
            "created_at": datetime.now(UTC),
        },
    )
    return {"extraction_id": extraction_id, "fields": fields, "model": model}


def locked_mismatch(record: dict[str, Any], sha256: str, submitted: dict[str, str]) -> str | None:
    """업로드 값이 자동 채움 기록과 다르면 사유를 돌려준다(없으면 None)."""
    if record.get("sha256") != sha256:
        return "자동 채움 뒤 파일이 바뀌었습니다. 취소 후 다시 선택해 주세요."
    for name, value in (record.get("fields") or {}).items():
        if submitted.get(name, "").strip() != value:
            return "자동으로 채운 칸은 수정할 수 없습니다. 취소 후 다시 선택해 주세요."
    return None

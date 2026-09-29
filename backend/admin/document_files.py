"""Validation and Cloud Storage operations for administrator-uploaded documents."""

from __future__ import annotations

import asyncio
import hashlib
import io
import zipfile
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any, Protocol

from backend.app.config import Settings, get_settings
from backend.domain import AppError

MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
HWP_OLE_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")


@dataclass(frozen=True, slots=True)
class ValidatedDocument:
    content: bytes
    format: str
    extension: str
    content_type: str
    sha256: str


class DocumentStorage(Protocol):
    async def upload_staging(self, document_id: str, document: ValidatedDocument) -> str: ...


def document_id_for_request(request_id: str) -> str:
    digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:24]
    return f"doc-{digest}"


def validate_document(filename: str | None, content: bytes) -> ValidatedDocument:
    if not content:
        raise AppError("BAD_REQUEST", "빈 문서는 업로드할 수 없습니다.")
    if len(content) > MAX_DOCUMENT_BYTES:
        raise AppError("BAD_REQUEST", "문서 크기는 20MB 이하여야 합니다.")
    extension = PurePath(filename or "").suffix.lower()
    content_type: str
    document_format: str
    if extension == ".pdf" and content.startswith(b"%PDF-"):
        document_format, content_type = "pdf", "application/pdf"
    elif extension == ".hwp" and content.startswith(HWP_OLE_MAGIC):
        document_format, content_type = "hwp", "application/x-hwp"
    elif extension in {".docx", ".hwpx"} and _valid_office_zip(content, extension):
        document_format = extension.removeprefix(".")
        content_type = (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            if extension == ".docx"
            else "application/vnd.hancom.hwpx"
        )
    elif extension in {".txt", ".md"}:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AppError("BAD_REQUEST", "텍스트 문서는 UTF-8이어야 합니다.") from exc
        if "\x00" in text:
            raise AppError("BAD_REQUEST", "텍스트 문서 형식이 올바르지 않습니다.")
        document_format, content_type = extension[1:], "text/plain; charset=utf-8"
    else:
        raise AppError("BAD_REQUEST", "지원 형식은 PDF, HWP, HWPX, DOCX, TXT, MD입니다.")
    return ValidatedDocument(
        content=content,
        format=document_format,
        extension=extension,
        content_type=content_type,
        sha256=f"sha256:{hashlib.sha256(content).hexdigest()}",
    )


def _valid_office_zip(content: bytes, extension: str) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = set(archive.namelist())
    except (OSError, zipfile.BadZipFile):
        return False
    marker = "word/document.xml" if extension == ".docx" else "Contents/content.hpf"
    return marker in names or (
        extension == ".hwpx" and any(name.startswith("Contents/section") for name in names)
    )


class GcsDocumentStorage:
    def __init__(self, settings: Settings | None = None, client: Any = None) -> None:
        from google.cloud import storage

        self.settings = settings or get_settings()
        self.client = client or storage.Client(project=self.settings.gcp_project_id or None)

    def _upload_sync(self, document_id: str, document: ValidatedDocument) -> str:
        if not self.settings.rules_bucket:
            raise ValueError("RULES_BUCKET is not configured")
        object_name = f"documents/staging/{document_id}/original{document.extension}"
        blob = self.client.bucket(self.settings.rules_bucket).blob(object_name)
        blob.metadata = {"sha256": document.sha256, "document_id": document_id}
        try:
            blob.upload_from_string(
                document.content,
                content_type=document.content_type,
                if_generation_match=0,
            )
        except Exception as exc:
            # A retried request may race with the first upload. Only accept the existing
            # object when its server-side metadata binds it to the same content hash.
            try:
                blob.reload()
            except Exception:
                raise exc from None
            if (blob.metadata or {}).get("sha256") != document.sha256:
                raise RuntimeError("staging object already exists with different content") from exc
        return object_name

    async def upload_staging(self, document_id: str, document: ValidatedDocument) -> str:
        return await asyncio.to_thread(self._upload_sync, document_id, document)


def get_document_storage() -> DocumentStorage:
    return GcsDocumentStorage()

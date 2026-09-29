from __future__ import annotations

import io
import zipfile

import pytest

from backend.admin.document_files import (
    MAX_DOCUMENT_BYTES,
    document_id_for_request,
    validate_document,
)
from backend.domain import AppError


def _zip_with(name: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(name, "<document />")
    return output.getvalue()


def test_validate_document_uses_extension_and_magic() -> None:
    pdf = validate_document("guide.pdf", b"%PDF-1.7\nbody")
    docx = validate_document("guide.docx", _zip_with("word/document.xml"))
    text = validate_document("guide.txt", "안내".encode())

    assert (pdf.format, docx.format, text.format) == ("pdf", "docx", "txt")
    assert text.sha256.startswith("sha256:")


def test_validate_document_rejects_mismatch_and_oversize() -> None:
    with pytest.raises(AppError):
        validate_document("guide.pdf", b"not a pdf")
    with pytest.raises(AppError):
        validate_document("guide.txt", b"x" * (MAX_DOCUMENT_BYTES + 1))


def test_document_id_is_stable_without_exposing_request_id() -> None:
    first = document_id_for_request("upload-request-1")

    assert first == document_id_for_request("upload-request-1")
    assert first.startswith("doc-")
    assert "upload-request" not in first

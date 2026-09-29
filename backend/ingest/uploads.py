"""교내 업로드 문서 → 본문 추출·분할·색인 문서(상세설계 01 §5 documents, 04 §6).

형식별 추출(업로드 시 서명 검사를 이미 통과한 파일만 들어온다):
- txt·md: UTF-8 그대로 / pdf: pypdf 텍스트(스캔 PDF는 텍스트 없음 → 미리보기 실패, 사람이 확인)
- hwp: hwp5txt / docx·hwpx: 압축 안 XML의 본문 텍스트(표·서식은 버림)
분할은 빈 줄 문단 단위로 모아 최대 1,500자.
"""

from __future__ import annotations

import io
import re
import tempfile
import zipfile
from pathlib import Path

MAX_CHUNK_CHARS = 1_500
MIN_TEXT_CHARS = 50
_TAG = re.compile(r"<[^>]+>")


class ExtractionError(Exception):
    """본문을 뽑지 못함 — 미리보기 실패(preview_error_code)로 기록한다."""


def _xml_text(xml: str, paragraph_tag: str) -> str:
    parts = re.split(rf"</{paragraph_tag}>", xml)
    lines = [_TAG.sub("", p).strip() for p in parts]
    return "\n".join(line for line in lines if line)


def extract_text(content: bytes, fmt: str) -> str:
    if fmt in ("txt", "md"):
        text = content.decode("utf-8")
    elif fmt == "pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        text = "\n\n".join((page.extract_text() or "") for page in reader.pages)
    elif fmt == "docx":
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            text = _xml_text(z.read("word/document.xml").decode("utf-8"), "w:p")
    elif fmt == "hwpx":
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            names = sorted(n for n in z.namelist() if n.startswith("Contents/section"))
            text = "\n\n".join(_xml_text(z.read(n).decode("utf-8"), "hp:p") for n in names)
    elif fmt == "hwp":
        from backend.ingest.rules import convert_hwp

        with tempfile.TemporaryDirectory(prefix="cb-upload-") as tmp:
            src = Path(tmp) / "upload.hwp"
            src.write_bytes(content)
            text = convert_hwp(src, output_dir=Path(tmp), output_stem="upload").text
    else:
        raise ExtractionError(f"unsupported format {fmt}")
    text = "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) < MIN_TEXT_CHARS:
        raise ExtractionError("no extractable text")  # 스캔 PDF 등
    return text


def chunk_text(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    chunks: list[str] = []
    current = ""
    for para in (p.strip() for p in text.split("\n\n")):
        if not para:
            continue
        while len(para) > max_chars:  # 문단 자체가 너무 길면 자른다
            if current:
                chunks.append(current)
                current = ""
            chunks.append(para[:max_chars])
            para = para[max_chars:]
        if current and len(current) + 2 + len(para) > max_chars:
            chunks.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current:
        chunks.append(current)
    return chunks


def build_upload_documents(doc_id: str, meta: dict[str, object], chunks: list[str]):
    """Vertex 문서 목록. Vertex ID upload-{doc}-{n}, 근거 ID(article_id) doc:{doc}:{n}."""
    title = str(meta.get("title") or "교내 문서")
    out = []
    for n, body in enumerate(chunks, start=1):
        data = {
            "article_id": f"doc:{doc_id}:{n}",
            # 긴급 회수 denylist는 원본 문서 단위다. 검색 청크에서도 부모를 직접
            # 확인할 수 있어야 archive/purge 직후 비동기 색인 삭제 전에도 숨겨진다.
            "parent_document_id": doc_id,
            "article_title": title if len(chunks) == 1 else f"{title} ({n}/{len(chunks)})",
            "rule_name": title,
            "kind": "upload",
            "source_kind": "guide",  # 학생 검색에서는 안내 자료로 취급
            "access": "public",
            "department": meta.get("department"),
            "revision_date": meta.get("effective_from"),
            "body": body,
            "index_version": f"upload-{meta.get('version', 1)}",
        }
        out.append(
            {"id": f"upload-{doc_id}-{n}", "structData": {k: v for k, v in data.items() if v}}
        )
    return out

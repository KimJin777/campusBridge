"""게시된 공지·행사의 본문을 검색 색인에 넣는다(교수님 2026-09-30: 그림 안내문도 파싱).

- 대상: campus_events 중 게시(active)된 공지·교내 행사(학사일정 제외).
- 본문이 글이면 그대로, 그림뿐이면(포스터·캡처) Gemini가 이미지 속 글자를 옮겨 적는다.
- 색인은 학사안내(guide)와 같은 형식: article_id = guide:notice-{event_id}:{n}
  → "해외봉사 지원 자격이 뭐예요?" 같은 질문에 공지 원문 링크와 함께 답한다.
- 숨김·대체되거나 종료 후 KEEP_DAYS가 지난 일정은 색인에서 뺀다.
- 한 번 실행에 새로 읽는 공지는 LIMIT건(기존 게시분은 매일 조금씩 채운다).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Any, Protocol

from backend.ingest.guides import build_guide_documents

LIMIT = 10
MAX_ATTEMPTS = 3
MIN_BODY_CHARS = 80  # 이보다 짧으면 그림 안내문으로 보고 이미지를 읽는다
MAX_TEXT_CHARS = 4000
CHUNK_CHARS = 1500
KEEP_DAYS = 30

TRANSCRIBE_PROMPT = (
    "대학 공지 본문의 이미지다. 이미지 속 글자를 보이는 그대로 한국어로 옮겨 적어라. "
    "제목·항목·날짜·장소·자격·신청 방법·문의처를 빠짐없이 적고, 표는 '항목: 내용' 한 줄씩 적는다. "
    "요약·해석·추측으로 내용을 더하지 말고, 글자가 없으면 빈 문자열만 답한다."
)


class Docs(Protocol):
    def merge(self, collection: str, doc_id: str, data: dict[str, Any]) -> None: ...
    def find(self, collection: str, field: str, value: Any) -> list[tuple[str, dict[str, Any]]]: ...


class Index(Protocol):
    def delete(self, vertex_ids: list[str]) -> int: ...


def _chunks(text: str) -> list[str]:
    out: list[str] = []
    buf = ""
    for line in text.splitlines():
        if buf and len(buf) + len(line) > CHUNK_CHARS:
            out.append(buf)
            buf = ""
        buf = f"{buf}\n{line}" if buf else line
    return [*out, buf] if buf else out


def _period(row: dict[str, Any]) -> str:
    label = row.get("date_label") or "기간"
    start, end = row.get("start_date"), row.get("end_date")
    return f"{label} {start} ~ {end}" if start and start != end else f"{label} {end}"


def build_documents(eid: str, row: dict[str, Any]) -> list[dict[str, object]]:
    title = str(row.get("title") or "공지")
    head = f"{title}\n({_period(row)} · 학교 공지 원문)"
    parts = _chunks(str(row.get("content_text") or ""))
    sections = [
        {
            "n": n,
            "heading": title if len(parts) == 1 else f"{title} ({n})",
            "body": f"{head}\n{p}",
            "links": [],
        }
        for n, p in enumerate(parts, start=1)
    ]
    fetched = dt.datetime.now(dt.UTC).isoformat()
    return build_guide_documents(f"notice-{eid}", str(row["source_url"]), title, sections, fetched)


def _expired(row: dict[str, Any], today: dt.date) -> bool:
    try:
        return dt.date.fromisoformat(row["end_date"]) < today - dt.timedelta(days=KEEP_DAYS)
    except (KeyError, TypeError, ValueError):
        return False


def index_event_notices(
    docs: Docs,
    index: Index,
    *,
    today: dt.date,
    now: Any,
    fetch_body: Callable[[str], str],
    fetch_images: Callable[[str], list[bytes]],
    transcribe: Callable[[list[bytes]], str | None],
    import_docs: Callable[[list[dict[str, object]]], bool],
) -> dict[str, int]:
    stats = {"read": 0, "poster": 0, "indexed": 0, "removed": 0, "failed": 0}
    left = LIMIT
    ready: list[tuple[str, dict[str, Any]]] = []
    for eid, row in docs.find("campus_events", "status", "active"):
        if row.get("source_type") == "calendar" or not row.get("source_url"):
            continue
        if _expired(row, today):
            continue
        if not row.get("content_checked_at"):
            if left <= 0 or int(row.get("content_attempts") or 0) >= MAX_ATTEMPTS:
                continue
            left -= 1
            try:
                text, source = fetch_body(row["source_url"]).strip(), "body"
                if len(text) < MIN_BODY_CHARS:
                    images = fetch_images(row["source_url"])
                    got = transcribe(images) if images else ""
                    if got is None:  # 일시 실패 → 다음 수집에서 다시
                        raise RuntimeError("transcribe failed")
                    text, source = f"{text}\n{got}".strip(), "poster"
                    stats["poster"] += 1
            except Exception:  # noqa: BLE001 — 공지 단위 격리
                stats["failed"] += 1
                docs.merge(
                    "campus_events",
                    eid,
                    {"content_attempts": int(row.get("content_attempts") or 0) + 1},
                )
                continue
            update = {
                "content_text": text[:MAX_TEXT_CHARS],
                "content_source": source,
                "content_checked_at": now,
            }
            docs.merge("campus_events", eid, update)
            row = {**row, **update}
            stats["read"] += 1
        if row.get("content_text") and not row.get("vertex_ids"):
            ready.append((eid, row))
    documents: list[dict[str, object]] = []
    planned: list[tuple[str, list[str]]] = []
    for eid, row in ready:
        page_docs = build_documents(eid, row)
        if page_docs:
            documents += page_docs
            planned.append((eid, [str(d["id"]) for d in page_docs]))
    if documents and import_docs(documents):
        for eid, ids in planned:
            docs.merge("campus_events", eid, {"vertex_ids": ids, "indexed_at": now})
            stats["indexed"] += 1
    for status in ("active", "disabled", "superseded"):
        for eid, row in docs.find("campus_events", "status", status):
            ids = list(row.get("vertex_ids") or [])
            if ids and (status != "active" or _expired(row, today)):
                index.delete(ids)
                docs.merge("campus_events", eid, {"vertex_ids": [], "removed_at": now})
                stats["removed"] += 1
    return stats


def live_transcribe(settings: Any) -> Callable[[list[bytes]], str | None]:
    """이미지 속 글자 옮겨 적기(Gemini). 실패는 None."""
    import asyncio
    import base64

    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_google_genai import ChatGoogleGenerativeAI

    from backend.ingest.events import _mime

    chat = ChatGoogleGenerativeAI(
        vertexai=True,
        model=settings.gemini_model,
        project=settings.gcp_project_id or None,
        location=settings.gemini_location,
        temperature=0,
        max_output_tokens=4096,
        max_retries=1,
        thinking_level="low",
    )

    def run(images: list[bytes]) -> str | None:
        parts: list[Any] = [{"type": "text", "text": "이 공지 이미지의 글자를 옮겨 적어 주세요."}]
        for data in images:
            parts.append(
                {
                    "type": "image",
                    "source_type": "base64",
                    "mime_type": _mime(data),
                    "data": base64.b64encode(data).decode(),
                }
            )
        try:
            msg = asyncio.run(
                asyncio.wait_for(
                    chat.ainvoke([SystemMessage(TRANSCRIBE_PROMPT), HumanMessage(content=parts)]),
                    timeout=120,
                )
            )
        except Exception:  # noqa: BLE001
            return None
        content = msg.content
        if isinstance(content, list):  # 일부 모델은 [{"type":"text","text":...}] 형태
            content = "".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
        return str(content or "").strip()

    return run

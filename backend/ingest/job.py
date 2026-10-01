"""수집 Job 진입점(상세설계 07 §8, 04 §6) — Cloud Run Job `campusbridge-ingest`에서 실행.

환경값(관리자 API가 실행 시 넘김)
- 변경분 재수집: INGESTION_RUN_ID, SOURCE_IDS(쉼표; 비면 규정+학사안내 전체)
- 교내 문서 처리: INGESTION_ACTION(preview|publish|archive|purge), DOCUMENT_ID

원칙
- 규정은 **변경분만**(목록 파일 버전이 manifest와 다를 때) 받아 검수 통과분만 INCREMENTAL 가져오기
- manifest는 컨테이너에 남지 않으므로 GCS `state/manifest.json`에서 읽고 쓴다
- 실행 상태는 Firestore `ingestion_runs/{run_id}`에 구조화 필드로만 기록(원시 로그는 Cloud Logging)

실행: uv run python -m backend.ingest.job
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger("campusbridge.job")

STATE_MANIFEST = "state/manifest.json"


# ── 외부 의존 경계(테스트에서 가짜로 바꾼다) ─────────────────────────────
class Blobs(Protocol):
    def read(self, name: str) -> bytes | None: ...
    def write(self, name: str, data: bytes, content_type: str) -> None: ...
    def delete_prefix(self, prefix: str) -> int: ...


class Docs(Protocol):
    def get(self, collection: str, doc_id: str) -> dict[str, Any] | None: ...
    def merge(self, collection: str, doc_id: str, data: dict[str, Any]) -> None: ...
    def find(self, collection: str, field: str, value: Any) -> list[tuple[str, dict[str, Any]]]: ...
    def transact(self, collection: str, doc_id: str, fn: Callable[[dict], dict]) -> dict: ...


class Index(Protocol):
    def import_jsonl(self, local_path: Path, object_prefix: str) -> list[str]: ...
    def delete(self, vertex_ids: list[str]) -> int: ...


@dataclass
class JobDeps:
    blobs: Blobs
    docs: Docs
    index: Index
    events: Callable[[], dict[str, int]] | None = None  # 일정 캘린더 수집(운영에서만 주입)
    menus: Callable[[], dict[str, int]] | None = None  # 식단 PDF → campus_menus(교수님 #634)
    # 게시된 공지·행사 본문(그림 안내문은 글자 판독) → 검색 색인(교수님 2026-09-30)
    event_docs: Callable[[Callable[[list[dict[str, object]]], bool]], dict[str, int]] | None = None


# 수집 중복 방지(2026-09-29 스케줄러 중복 실행 사고): 다른 수집이 이 시간 안에 갱신됐으면 건너뛴다
ACTIVE_STATUSES = ("queued", "running")
STALE_AFTER = dt.timedelta(hours=2)


def run_blocks(active: dict[str, Any] | None, now: dt.datetime) -> bool:
    """진행 중(최근 갱신)인 다른 수집이 있으면 True — 이번 실행은 건너뛴다."""
    if not active or active.get("status") not in ACTIVE_STATUSES:
        return False
    updated = active.get("updated_at") or active.get("started_at")
    if not isinstance(updated, dt.datetime):
        return False
    return now - updated < STALE_AFTER


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# ── 교내 문서 처리 ─────────────────────────────────────────────────────
def process_document(action: str, doc_id: str, deps: JobDeps) -> dict[str, Any]:
    from backend.ingest.uploads import (
        build_upload_documents,
        chunk_text,
        extract_text,
    )

    doc = deps.docs.get("documents", doc_id)
    if doc is None:
        raise RuntimeError("document not found")
    chunks_path = f"documents/staging/{doc_id}/chunks.json"

    if action == "preview":
        content = deps.blobs.read(str(doc.get("gcs_staging_path") or ""))
        if content is None:
            return _doc_fail(deps, doc_id, "preview", "SOURCE_MISSING")
        try:
            text = extract_text(content, str(doc.get("format")))
        except Exception:  # noqa: BLE001 — 형식별 라이브러리 예외 전부(깨진 PDF 등)
            return _doc_fail(deps, doc_id, "preview", "EXTRACTION_FAILED")
        chunks = chunk_text(text)
        deps.blobs.write(
            chunks_path, json.dumps(chunks, ensure_ascii=False).encode(), "application/json"
        )
        changes = {
            "preview_status": "ready",
            "preview_chunk_count": len(chunks),
            "preview_text_length": len(text),
            "preview_chunks": [c[:500] for c in chunks[:5]],
            "preview_error_code": None,
            "updated_at": _now(),
        }
        deps.docs.merge("documents", doc_id, changes)
        return changes

    if action == "publish":
        if doc.get("status") != "publishing":
            return _doc_fail(deps, doc_id, "publish", "INVALID_STATE")
        raw = deps.blobs.read(chunks_path)
        if raw is None:
            return _doc_fail(deps, doc_id, "publish", "PREVIEW_MISSING")
        documents = build_upload_documents(doc_id, doc, json.loads(raw))
        with tempfile.TemporaryDirectory(prefix="cb-publish-") as tmp:
            path = Path(tmp) / f"{doc_id}.jsonl"
            path.write_text(
                "\n".join(json.dumps(d, ensure_ascii=False) for d in documents) + "\n",
                encoding="utf-8",
            )
            errors = deps.index.import_jsonl(path, object_prefix=f"uploads/{doc_id}")
        if errors:
            return _doc_fail(deps, doc_id, "publish", "IMPORT_FAILED")
        changes = {
            "status": "published",
            "indexed_ids": [d["id"] for d in documents],
            "published_index_at": _now(),
            "job_error_code": None,
            "updated_at": _now(),
        }
        deps.docs.merge("documents", doc_id, changes)
        return changes

    if action in ("archive", "purge"):
        removed = deps.index.delete(list(doc.get("indexed_ids") or []))
        changes: dict[str, Any] = {"index_removed": removed, "updated_at": _now()}
        if action == "purge":
            deleted = deps.blobs.delete_prefix(f"documents/staging/{doc_id}/")
            deleted += deps.blobs.delete_prefix(f"documents/approved/{doc_id}/")
            changes.update(
                {
                    "status": "purged",
                    "blobs_deleted": deleted,
                    "preview_chunks": [],
                    "gcs_staging_path": None,
                    "indexed_ids": [],
                }
            )
        deps.docs.merge("documents", doc_id, changes)
        return changes

    raise RuntimeError(f"unsupported action {action}")


def _doc_fail(deps: JobDeps, doc_id: str, action: str, code: str) -> dict[str, Any]:
    field = "preview_status" if action == "preview" else "job_status"
    changes = {
        field: "failed",
        "preview_error_code" if action == "preview" else "job_error_code": code,
        "updated_at": _now(),
    }
    deps.docs.merge("documents", doc_id, changes)
    return changes


def process_phonebook(object_name: str, deps: JobDeps, apply_fn) -> dict[str, Any]:
    """관리자가 올린 전화번호부(HWP·PDF)를 변환·파싱해 directory_entries를 통째로 교체한다."""
    from backend.ingest.phonebook import parse_phonebook
    from backend.ingest.uploads import extract_text

    content = deps.blobs.read(object_name)
    if content is None:
        return _phonebook_state(deps, "failed", error_code="SOURCE_MISSING")
    fmt = object_name.rsplit(".", 1)[-1].lower()
    try:
        entries = parse_phonebook(extract_text(content, fmt))
    except Exception:  # noqa: BLE001 — 형식별 변환 예외 전부
        return _phonebook_state(deps, "failed", error_code="EXTRACTION_FAILED")
    if len(entries) < 20 or sum(1 for e in entries if e.phone) < 10:
        # 표 번호가 빠진 PDF 등 — 기존 전화번호부를 지우지 않고 실패로 남긴다
        return _phonebook_state(deps, "failed", error_code="TOO_FEW_ENTRIES", parsed=len(entries))
    stats = apply_fn(entries, source=object_name)
    return {"status": "applied", **stats}


def _phonebook_state(deps: JobDeps, status: str, **fields: Any) -> dict[str, Any]:
    data = {"status": status, "updated_at": _now(), **fields}
    deps.docs.merge("source_configs", "phonebook", data)
    return data


# ── 변경분 재수집 ──────────────────────────────────────────────────────
def run_ingestion(
    run_id: str, source_ids: list[str], deps: JobDeps, workdir: Path
) -> dict[str, Any]:
    from backend.ingest.download import download_rules
    from backend.ingest.full_index import build_documents
    from backend.ingest.indexing import _write_jsonl
    from backend.ingest.manifest import load_manifest
    from backend.ingest.registry import collect_rules

    wanted = set(source_ids) or {"rules", "academic_guides", "web_pages", "events", "menus"}
    stats: dict[str, Any] = {"added": 0, "changed": 0, "rejected": 0, "processed": 0, "total": 0}
    _run(deps, run_id, status="running", phase="registry", started_at=_now())

    if "rules" in wanted:
        manifest = workdir / "manifest.json"
        raw = deps.blobs.read(STATE_MANIFEST)
        if raw:
            manifest.write_bytes(raw)
        before = load_manifest(manifest)
        records = asyncio.run(collect_rules())
        changed = [
            r
            for r in records
            if r.rule_no not in before or before[r.rule_no].file_version != r.file_version
        ]
        stats["total"] = len(changed)
        _run(deps, run_id, phase="download", total=len(changed), processed=0)
        if changed:
            asyncio.run(download_rules(changed, output_dir=workdir / "raw", manifest_path=manifest))
            _run(deps, run_id, phase="convert")
            documents, per_rule = build_documents(
                changed,
                manifest_path=manifest,
                raw_dir=workdir / "raw",
                interim_dir=workdir / "tmp",
            )
            statuses = [v["status"] for v in per_rule.values()]  # type: ignore[index]
            stats["rejected"] = statuses.count("rejected") + statuses.count("missing_source")
            stats["changed"] = sum(1 for r in changed if r.rule_no in before)
            stats["added"] = len(changed) - stats["changed"]
            stats["processed"] = len(changed)
            if documents:
                _run(deps, run_id, phase="import")
                out = workdir / "articles.jsonl"
                _write_jsonl(out, documents)
                if deps.index.import_jsonl(out, object_prefix=f"runs/{run_id}"):
                    raise RuntimeError("IMPORT_FAILED")
        deps.blobs.write(STATE_MANIFEST, manifest.read_bytes(), "application/json")
        after = load_manifest(manifest)
        deps.docs.merge(
            "source_configs",
            "rules",
            {
                "kind": "rule",
                "last_success_at": _now(),
                "doc_count": sum(e.article_count or 0 for e in after.values()),
                "rejected_rule_nos": sorted(k for k, e in after.items() if e.status == "rejected"),
                "active_index_version": "articles-v1",
            },
        )

    if "academic_guides" in wanted:
        from backend.ingest.guides import collect, load_pages

        _run(deps, run_id, phase="guides")
        pages, hosts = load_pages(Path("config/sources.yaml"))
        docs, _ = asyncio.run(collect(pages, hosts))
        if docs:
            out = workdir / "guides.jsonl"
            _write_jsonl(out, docs)
            if deps.index.import_jsonl(out, object_prefix=f"runs/{run_id}/guides"):
                raise RuntimeError("IMPORT_FAILED")

    if "web_pages" in wanted and _web_pages_enabled():
        from backend.ingest.webpage import collect_registered

        _run(deps, run_id, phase="web_pages")

        def import_docs(documents: list[dict[str, object]]) -> bool:
            out = workdir / "web_pages.jsonl"
            _write_jsonl(out, documents)
            return not deps.index.import_jsonl(out, object_prefix=f"runs/{run_id}/web")

        try:
            stats["web_pages"] = collect_registered(
                deps.docs, deps.index, now=_now(), import_docs=import_docs
            )
        except Exception as exc:  # noqa: BLE001 — 등록 페이지 실패가 다른 수집을 막지 않게
            log.exception("web page collection failed")
            stats["web_pages_error"] = type(exc).__name__

    # 메뉴 순회로 학생 생활 페이지 후보 찾기(#909) — "crawl"을 명시하고 WEB_CRAWL_ENABLED=1일 때만
    if "crawl" in set(source_ids):
        from backend.ingest.crawl import crawl, crawl_enabled, save_candidates

        if not crawl_enabled():
            stats["crawl"] = {"skipped": "disabled"}
        else:
            from urllib.parse import urljoin

            from backend.ingest.guides import BASE_URL, load_pages
            from backend.ingest.webpage import fetch_html

            _run(deps, run_id, phase="crawl")
            known = {str(r.get("url")) for _, r in deps.docs.find("web_pages", "status", "active")}
            guide_pages, _hosts = load_pages(Path("config/sources.yaml"))
            known |= {urljoin(BASE_URL, p["path"]) for p in guide_pages}
            found = crawl(lambda u: asyncio.run(fetch_html(u)), known=known)
            stats["crawl"] = {
                "pages": found["pages"],
                "candidates": save_candidates(deps.docs, found, _now()),
                "stopped": found["stopped"],
            }

    if "events" in wanted and deps.events is not None:
        _run(deps, run_id, phase="events")
        try:
            stats["events"] = deps.events()
        except Exception as exc:  # noqa: BLE001 — 일정 수집 실패가 규정 수집 결과를 막지 않게
            log.exception("events collection failed")
            stats["events_error"] = type(exc).__name__

    if "events" in wanted and deps.event_docs is not None:
        _run(deps, run_id, phase="event_docs")

        def import_event_docs(documents: list[dict[str, object]]) -> bool:
            out = workdir / "event_docs.jsonl"
            _write_jsonl(out, documents)
            return not deps.index.import_jsonl(out, object_prefix=f"runs/{run_id}/notices")

        try:
            stats["event_docs"] = deps.event_docs(import_event_docs)
        except Exception as exc:  # noqa: BLE001 — 본문 색인 실패가 다른 수집을 막지 않게
            log.exception("event docs indexing failed")
            stats["event_docs_error"] = type(exc).__name__

    if "events" in wanted:  # 매일 1회 꿀팁 시간 전이(24시간 경과 승격 — GPT5 #717-1)
        try:
            stats["tips"] = _reconcile_tips(deps)
        except Exception as exc:  # noqa: BLE001 — 꿀팁 판정 실패가 수집을 막지 않게
            log.exception("tips reconcile failed")
            stats["tips_error"] = type(exc).__name__

    if "menus" in wanted and deps.menus is not None:
        _run(deps, run_id, phase="menus")
        try:
            stats["menus"] = deps.menus()
        except Exception as exc:  # noqa: BLE001 — 식단 실패가 다른 수집 결과를 막지 않게
            log.exception("menu collection failed")
            stats["menus_error"] = type(exc).__name__

    _run(deps, run_id, status="success", phase="done", finished_at=_now(), **stats)
    return stats


def _reconcile_tips(deps: JobDeps) -> dict[str, int]:
    """검증 중 꿀팁의 시간 전이. 후보는 목록에서 고르되, 쓰기는 문서마다 트랜잭션으로 다시
    읽어 여전히 verifying일 때만 판정한다(관리자 결정·새 반대표를 덮어쓰지 않음 — GPT5 #722-5)."""
    from backend.reports.rules import evaluate_tip, reconcile_tips

    now = _now()
    rows = [{"id": i, **r} for i, r in deps.docs.find("reports", "type", "tip")]
    changed = 0
    for tip_id in reconcile_tips(rows, now):

        def fn(current: dict[str, Any]) -> dict[str, Any]:
            if current.get("status") != "verifying":
                return {}
            change = evaluate_tip(current, now)
            return {**change, "updated_at": now, "reconciled_by": "job"} if change else {}

        if deps.docs.transact("reports", tip_id, fn):
            changed += 1
    return {"checked": len(rows), "changed": changed}


def _web_pages_enabled() -> bool:
    return os.getenv("WEB_PAGES_ENABLED", "1").lower() not in ("0", "false", "off")


def _run(deps: JobDeps, run_id: str, **fields: Any) -> None:
    deps.docs.merge("ingestion_runs", run_id, {**fields, "updated_at": _now()})


# ── 실제 GCP 구현 ─────────────────────────────────────────────────────
class GcsBlobs:
    def __init__(self, project: str, bucket: str):
        from google.cloud import storage

        self.bucket = storage.Client(project=project).bucket(bucket)

    def read(self, name: str) -> bytes | None:
        if not name:
            return None
        blob = self.bucket.blob(name)
        return blob.download_as_bytes() if blob.exists() else None

    def write(self, name: str, data: bytes, content_type: str) -> None:
        self.bucket.blob(name).upload_from_string(data, content_type=content_type)

    def delete_prefix(self, prefix: str) -> int:
        blobs = list(self.bucket.list_blobs(prefix=prefix))
        for b in blobs:
            b.delete()
        return len(blobs)


class FirestoreDocs:
    def __init__(self, project: str, database: str):
        from google.cloud import firestore

        self.db = firestore.Client(project=project, database=database)

    def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        snap = self.db.collection(collection).document(doc_id).get()
        return snap.to_dict() if snap.exists else None

    def merge(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        self.db.collection(collection).document(doc_id).set(data, merge=True)

    def transact(self, collection: str, doc_id: str, fn) -> dict[str, Any]:
        """문서를 트랜잭션으로 다시 읽어 fn(현재값)의 변경분만 쓴다(경쟁 상태 방지)."""
        from google.cloud import firestore

        ref = self.db.collection(collection).document(doc_id)

        @firestore.transactional
        def txn(tx) -> dict[str, Any]:
            snap = ref.get(transaction=tx)
            change = fn(snap.to_dict() or {}) if snap.exists else {}
            if change:
                tx.set(ref, change, merge=True)
            return change

        return txn(self.db.transaction())

    def find(self, collection: str, field: str, value: Any) -> list[tuple[str, dict[str, Any]]]:
        """단일 필드 동등 조회(정정 reconcile용 — 단일 필드 자동 색인으로 충분)."""
        from google.cloud.firestore_v1.base_query import FieldFilter

        q = self.db.collection(collection).where(filter=FieldFilter(field, "==", value))
        return [(d.id, d.to_dict() or {}) for d in q.stream()]

    def claim_run(self, run_id: str) -> bool:
        """ingestion_control/active를 트랜잭션으로 선점한다. 다른 수집이 진행 중이면 False."""
        from google.cloud import firestore

        ctrl = self.db.collection("ingestion_control").document("active")

        @firestore.transactional
        def txn(tx) -> bool:
            snap = ctrl.get(transaction=tx)
            active_id = (snap.to_dict() or {}).get("run_id") if snap.exists else None
            if active_id == run_id:  # 관리자 화면이 선점하고 디스패치한 실행
                return True
            if active_id:
                a = self.db.collection("ingestion_runs").document(active_id).get(transaction=tx)
                if run_blocks(a.to_dict() if a.exists else None, _now()):
                    return False
            tx.set(ctrl, {"run_id": run_id, "updated_at": _now()}, merge=True)
            return True

        return txn(self.db.transaction())


class VertexIndex:
    def __init__(self, project: str, bucket: str, data_store_id: str, engine_id: str):
        self.project, self.bucket = project, bucket
        self.data_store_id, self.engine_id = data_store_id, engine_id

    def import_jsonl(self, local_path: Path, object_prefix: str) -> list[str]:
        from backend.ingest.full_index import import_index

        result = import_index(
            project=self.project,
            bucket=self.bucket,
            data_store_id=self.data_store_id,
            engine_id=self.engine_id,
            source=local_path,
            object_prefix=object_prefix,
            ensure_resources=False,  # ingest-sa는 스키마 수정 권한 없음(2026-09-29 첫 실행 403)
        )
        return list(result["errors"])

    def delete(self, vertex_ids: list[str]) -> int:
        from google.api_core.exceptions import NotFound

        from backend.ingest.search_spike import _clients

        _, _, _, client, _ = _clients()
        parent = (
            f"projects/{self.project}/locations/global/collections/default_collection/"
            f"dataStores/{self.data_store_id}/branches/default_branch/documents"
        )
        removed = 0
        for vid in vertex_ids:
            try:
                client.delete_document(name=f"{parent}/{vid}")
                removed += 1
            except NotFound:
                pass
        return removed


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    project = os.environ["GCP_PROJECT_ID"]
    bucket = os.environ["RULES_BUCKET"]
    deps = JobDeps(
        blobs=GcsBlobs(project, bucket),
        docs=FirestoreDocs(project, os.environ.get("FIRESTORE_DB", "campusbridge")),
        index=VertexIndex(
            project,
            bucket,
            os.environ.get("SEARCH_DATASTORE_ID", "rules-articles"),
            os.environ.get("SEARCH_ENGINE_ID", "rules-articles-search"),
        ),
    )
    action, doc_id = os.environ.get("INGESTION_ACTION"), os.environ.get("DOCUMENT_ID")
    if action == "phonebook" and doc_id:
        from backend.ingest.phonebook import apply_directory

        db = deps.docs.db  # type: ignore[attr-defined]
        result = process_phonebook(
            doc_id,
            deps,
            lambda entries, source: apply_directory(entries, db, source=source, actor="job"),
        )
        log.info(json.dumps({"event": "phonebook_job", "result": str(result)[:500]}))
        return 0 if result.get("status") == "applied" else 1
    if action and doc_id:
        result = process_document(action, doc_id, deps)
        log.info(
            json.dumps({"event": "document_job", "action": action, "result": str(result)[:500]})
        )
        return 0
    dispatched = os.environ.get("INGESTION_RUN_ID")
    run_id = dispatched or f"schedule-{_now():%Y%m%dT%H%M%S}"
    sources = [s for s in os.environ.get("SOURCE_IDS", "").split(",") if s.strip()]
    if not deps.docs.claim_run(run_id):  # type: ignore[attr-defined]
        log.info(json.dumps({"event": "ingestion_skipped", "run_id": run_id, "reason": "active"}))
        return 0
    if not dispatched:
        _run(deps, run_id, trigger="schedule", source_ids=sources, created_at=_now())

    from backend.app.config import get_settings
    from backend.ingest.events import (
        KST,
        collect_events,
        live_body,
        live_event_check,
        live_images,
        live_llm,
        live_notice_poster,
        live_poster_check,
        live_sources,
    )

    settings = get_settings()

    def events() -> dict[str, int]:
        cal, notices = live_sources(settings)
        now = _now()
        out = collect_events(
            deps.docs,
            today=now.astimezone(KST).date(),
            now=now,
            fetch_calendar=cal,
            fetch_notices=notices,
            llm_extract=live_llm(settings),
            fetch_body=live_body(settings),
            check_event=live_event_check(settings),
            fetch_images=live_images(settings),
            check_poster=live_poster_check(settings),
            extract_poster=live_notice_poster(settings),
        )
        deps.docs.merge(
            "source_configs",
            "academic_calendar",
            {"kind": "calendar", "last_success_at": now, "doc_count": sum(out.values())},
        )
        return out

    deps.events = events

    from backend.ingest.notice_docs import MAX_TEXT_CHARS, index_event_notices, live_transcribe

    def event_docs(import_docs) -> dict[str, int]:
        from bs4 import BeautifulSoup

        from backend.tools import public_sources as ps

        def fetch_body(url: str) -> str:
            html = asyncio.run(ps._fetch(url, settings))
            node = BeautifulSoup(html, "html.parser").select_one(".view-con")
            return node.get_text("\n", strip=True)[:MAX_TEXT_CHARS] if node else ""

        now = _now()
        return index_event_notices(
            deps.docs,
            deps.index,
            today=now.astimezone(KST).date(),
            now=now,
            fetch_body=fetch_body,
            fetch_images=live_images(settings),
            transcribe=live_transcribe(settings),
            import_docs=import_docs,
        )

    deps.event_docs = event_docs

    from backend.ingest.menus import (
        collect_menus,
        live_layout_llm,
        live_menu_sources,
        live_ocr,
    )

    def menus() -> dict[str, int]:
        list_weeks, download = live_menu_sources(settings)
        now = _now()
        return collect_menus(
            deps.docs,
            today=now.astimezone(KST).date(),
            now=now,
            list_weeks=list_weeks,
            download=download,
            layout_llm=live_layout_llm(settings),
            ocr=live_ocr(settings),
        )

    deps.menus = menus
    try:
        with tempfile.TemporaryDirectory(prefix="cb-ingest-") as tmp:
            run_ingestion(run_id, sources, deps, Path(tmp))
    except Exception as exc:  # noqa: BLE001 — 구조화 상태만 남기고 실패 종료
        log.exception("ingestion failed")
        code = str(exc) if str(exc).isupper() else type(exc).__name__
        _run(deps, run_id, status="failed", error_code=code[:60], finished_at=_now())
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

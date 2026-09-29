"""Authenticated operational endpoints for CampusBridge administrators."""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from fastapi import APIRouter, Depends, File, Form, Query, UploadFile

from backend.admin.auth import AdminActor, invalidate_admin_cache, require_admin
from backend.admin.document_files import (
    DocumentStorage,
    document_id_for_request,
    get_document_storage,
    validate_document,
)
from backend.admin.jobs import JobLauncher, get_job_launcher
from backend.admin.models import (
    AdminAddRequest,
    AdminRemoveRequest,
    DisableRequest,
    DocumentActionRequest,
    EventPatch,
    IngestionRunRequest,
    SourcePatch,
    normalize_admin_email,
)
from backend.admin.places import PlaceCreate, PlacePatch
from backend.admin.source_links import source_links
from backend.admin.store import AdminStore, get_admin_store
from backend.app.config import Settings, get_settings
from backend.domain import AppError

router = APIRouter(prefix="/api/admin", tags=["admin"])
Actor = Annotated[AdminActor, Depends(require_admin)]
Store = Annotated[AdminStore, Depends(get_admin_store)]
Launcher = Annotated[JobLauncher, Depends(get_job_launcher)]
Documents = Annotated[DocumentStorage, Depends(get_document_storage)]
Limit = Annotated[int, Query(ge=1, le=200)]
Cursor = Annotated[str | None, Query(max_length=200)]
CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def _valid_id(value: str) -> str:
    clean = value.strip()
    if not clean or len(clean) > 200 or "/" in clean or clean in {".", ".."}:
        raise AppError("BAD_REQUEST", "식별자 형식이 올바르지 않습니다.")
    return clean


def _required_text(value: str, field_name: str) -> str:
    clean = value.strip()
    if not clean:
        raise AppError("BAD_REQUEST", f"{field_name} 항목은 비워 둘 수 없습니다.")
    return clean


def _admin_email(value: str) -> str:
    try:
        return normalize_admin_email(value)
    except ValueError as exc:
        raise AppError("BAD_REQUEST", "이메일 형식이 올바르지 않습니다.") from exc


def _document_view(item: dict[str, Any], *, include_preview: bool = False) -> dict[str, Any]:
    allowed = {
        "id",
        "title",
        "department",
        "doc_date",
        "effective_from",
        "source",
        "approval_basis",
        "expires_at_doc",
        "status",
        "preview_status",
        "preview_error_code",
        "last_action",
        "job_action",
        "job_error_code",
        "version",
        "previous_version_id",
        "sha256",
        "format",
        "size_bytes",
        "created_at",
        "updated_at",
    }
    if include_preview:
        allowed.update({"preview_chunks", "preview_chunk_count", "preview_text_length"})
    return {key: value for key, value in item.items() if key in allowed}


@router.get("/sources")
async def list_sources(
    actor: Actor,
    store: Store,
    limit: Limit = 100,
    cursor: Cursor = None,
) -> dict[str, Any]:
    del actor
    page = await store.list_page(
        "source_configs", limit=limit, cursor=cursor, order_by="kind", descending=False
    )
    links = source_links()
    for item in page["items"]:
        item["links"] = links.get(str(item.get("id")), [])
    return page


@router.get("/admins")
async def list_admins(
    actor: Actor,
    store: Store,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    page = await store.list_page(
        "admin_users", limit=200, cursor=None, order_by="email", descending=False
    )
    by_email: dict[str, dict[str, Any]] = {}
    for row in page["items"]:
        email = str(row.get("email") or row.get("id") or "").strip().lower()
        if email:
            by_email[email] = {**row, "email": email, "bootstrap": False}
    for email in settings.admin_emails:
        row = by_email.get(email, {"email": email})
        by_email[email] = {**row, "email": email, "status": "active", "bootstrap": True}
    items = sorted(by_email.values(), key=lambda row: (not row["bootstrap"], row["email"]))
    return {"items": items, "current_email": actor.email}


@router.post("/admins", status_code=201)
async def add_admin(
    body: AdminAddRequest,
    actor: Actor,
    store: Store,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    if body.email in settings.admin_emails:
        raise AppError("BAD_REQUEST", "부트스트랩 관리자는 이미 등록되어 있습니다.")
    item, reused = await store.add_admin_user(
        body.email,
        note=body.note,
        actor=actor,
        reason=body.reason,
        request_id=body.request_id,
    )
    invalidate_admin_cache()
    return {**item, "bootstrap": False, "reused": reused}


@router.post("/admins/{email}/remove")
async def remove_admin(
    email: str,
    body: AdminRemoveRequest,
    actor: Actor,
    store: Store,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    target = _admin_email(email)
    if target in settings.admin_emails:
        raise AppError("BAD_REQUEST", "부트스트랩 관리자는 삭제할 수 없습니다.")
    if target == actor.email:
        raise AppError("BAD_REQUEST", "현재 로그인한 관리자 자신은 삭제할 수 없습니다.")
    item, reused = await store.remove_admin_user(
        target,
        actor=actor,
        reason=body.reason,
        request_id=body.request_id,
    )
    invalidate_admin_cache()
    return {**item, "bootstrap": False, "reused": reused}


@router.patch("/sources/{source_id}")
async def patch_source(
    source_id: str,
    body: SourcePatch,
    actor: Actor,
    store: Store,
) -> dict[str, Any]:
    changes = body.model_dump(include={"schedule", "paused"}, exclude_none=True)
    if not changes:
        raise AppError("BAD_REQUEST", "변경할 설정이 없습니다.")
    return await store.patch_source(
        _valid_id(source_id),
        changes,
        actor=actor,
        reason=body.reason,
        request_id=body.request_id,
    )


@router.post("/disable")
async def disable_documents(
    body: DisableRequest,
    actor: Actor,
    store: Store,
) -> dict[str, Any]:
    disabled = await store.disable_documents(
        body.document_ids,
        actor=actor,
        reason=body.reason,
        request_id=body.request_id,
    )
    return {
        "disabled_document_ids": disabled,
        "vertex_status": "pending",
        "message": "검색 선필터에 즉시 반영했으며 색인 비활성화는 진행 중입니다.",
    }


@router.get("/documents")
async def list_documents(
    actor: Actor, store: Store, limit: Limit = 50, cursor: Cursor = None
) -> dict[str, Any]:
    del actor
    page = await store.list_page("documents", limit=limit, cursor=cursor, order_by="updated_at")
    return {**page, "items": [_document_view(i) for i in page["items"]]}


@router.get("/ingestion-runs")
async def list_ingestion_runs(
    actor: Actor, store: Store, limit: Limit = 20, cursor: Cursor = None
) -> dict[str, Any]:
    del actor
    page = await store.list_page(
        "ingestion_runs", limit=limit, cursor=cursor, order_by="updated_at"
    )
    return {**page, "items": [_ingestion_run_view(i) for i in page["items"]]}


@router.get("/unanswered")
async def list_unanswered(
    actor: Actor,
    store: Store,
    limit: Limit = 50,
    cursor: Cursor = None,
) -> dict[str, Any]:
    del actor
    return await store.list_page("unanswered", limit=limit, cursor=cursor, order_by="last_at")


@router.get("/feedback")
async def list_feedback(
    actor: Actor,
    store: Store,
    limit: Limit = 50,
    cursor: Cursor = None,
) -> dict[str, Any]:
    del actor
    return await store.list_page("feedback", limit=limit, cursor=cursor, order_by="created_at")


@router.get("/stats")
async def get_stats(
    actor: Actor,
    store: Store,
    days: Annotated[int, Query(ge=1, le=90)] = 30,
) -> dict[str, Any]:
    del actor
    return await store.stats(days)


@router.get("/ingestion-runs/{run_id}")
async def get_ingestion_run(
    run_id: str,
    actor: Actor,
    store: Store,
) -> dict[str, Any]:
    del actor
    target = _valid_id(run_id)
    item = await store.get_document("ingestion_runs", target)
    if item is None:
        raise AppError("BAD_REQUEST", "수집 실행 기록을 찾지 못했습니다.")
    allowed = {
        "id",
        "status",
        "phase",
        "processed",
        "total",
        "added",
        "changed",
        "rejected",
        "started_at",
        "finished_at",
        "error_code",
        "message",
    }
    return {key: value for key, value in item.items() if key in allowed}


@router.post("/ingestion-runs", status_code=202)
async def start_ingestion_run(
    body: IngestionRunRequest,
    actor: Actor,
    store: Store,
    launcher: Launcher,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    if not settings.gcp_project_id or not settings.ingestion_job_name:
        raise AppError("BAD_REQUEST", "수집 Job 설정이 없어 실행할 수 없습니다.")

    run, created = await store.create_ingestion_run(
        body.source_ids,
        idempotency_key=body.idempotency_key,
        actor=actor,
    )
    if not created:
        return {**_ingestion_run_view(run), "reused": True}

    run_id = str(run["id"])
    try:
        operation_name = await launcher.start(run_id, body.source_ids)
    except Exception as exc:
        await store.mark_ingestion_launch(
            run_id,
            actor=actor,
            operation_name=None,
            error_code="JOB_START_FAILED",
        )
        raise AppError("INTERNAL", "수집 Job을 시작하지 못했습니다.") from exc

    launched = await store.mark_ingestion_launch(
        run_id,
        actor=actor,
        operation_name=operation_name,
    )
    return {**_ingestion_run_view(launched), "reused": False}


def _ingestion_run_view(item: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "id",
        "source_ids",
        "status",
        "phase",
        "processed",
        "total",
        "added",
        "changed",
        "rejected",
        "started_at",
        "finished_at",
        "error_code",
    }
    return {key: value for key, value in item.items() if key in allowed}


@router.post("/documents", status_code=202)
async def upload_document(
    actor: Actor,
    store: Store,
    launcher: Launcher,
    storage: Documents,
    settings: Annotated[Settings, Depends(get_settings)],
    file: Annotated[UploadFile, File()],
    title: Annotated[str, Form(min_length=1, max_length=200)],
    department: Annotated[str, Form(min_length=1, max_length=100)],
    doc_date: Annotated[date, Form()],
    effective_from: Annotated[date, Form()],
    source: Annotated[str, Form(min_length=1, max_length=300)],
    approval_basis: Annotated[str, Form(min_length=1, max_length=500)],
    expires_at_doc: Annotated[date, Form()],
    reason: Annotated[str, Form(min_length=1, max_length=300)],
    request_id: Annotated[str, Form(min_length=8, max_length=100)],
) -> dict[str, Any]:
    if not settings.rules_bucket or not settings.gcp_project_id or not settings.ingestion_job_name:
        raise AppError("BAD_REQUEST", "문서 저장소 또는 변환 Job 설정이 없습니다.")
    if expires_at_doc < effective_from:
        raise AppError("BAD_REQUEST", "문서 만료일은 시행일보다 빠를 수 없습니다.")
    content = await file.read(20 * 1024 * 1024 + 1)
    document = validate_document(file.filename, content)
    document_id = document_id_for_request(request_id.strip())
    staging_path = await storage.upload_staging(document_id, document)
    item, created = await store.create_document(
        document_id,
        {
            "title": _required_text(title, "제목"),
            "department": _required_text(department, "소관부서"),
            "doc_date": doc_date.isoformat(),
            "effective_from": effective_from.isoformat(),
            "source": _required_text(source, "출처"),
            "approval_basis": _required_text(approval_basis, "승인 근거"),
            "expires_at_doc": expires_at_doc.isoformat(),
            "sha256": document.sha256,
            "format": document.format,
            "size_bytes": len(document.content),
            "gcs_staging_path": staging_path,
        },
        actor=actor,
        reason=_required_text(reason, "변경 사유"),
        request_id=request_id.strip(),
    )
    if not created and item.get("preview_status") in {"processing", "ready"}:
        return {**_document_view(item), "reused": True}
    try:
        operation_name = await launcher.start_document(document_id, "preview")
    except Exception as exc:
        await store.mark_document_preview(
            document_id,
            actor=actor,
            request_id=request_id.strip(),
            operation_name=None,
            error_code="PREVIEW_JOB_START_FAILED",
        )
        raise AppError("INTERNAL", "문서 미리보기 작업을 시작하지 못했습니다.") from exc
    item = await store.mark_document_preview(
        document_id,
        actor=actor,
        request_id=request_id.strip(),
        operation_name=operation_name,
    )
    return {**_document_view(item), "reused": not created}


@router.get("/documents/{document_id}/preview")
async def get_document_preview(
    document_id: str,
    actor: Actor,
    store: Store,
) -> dict[str, Any]:
    del actor
    item = await store.get_document("documents", _valid_id(document_id))
    if item is None:
        raise AppError("BAD_REQUEST", "문서를 찾지 못했습니다.")
    return _document_view(item, include_preview=True)


@router.post("/documents/{document_id}/{action}")
async def document_action(
    document_id: str,
    action: Literal["publish", "reject", "archive", "purge"],
    body: DocumentActionRequest,
    actor: Actor,
    store: Store,
    launcher: Launcher,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """문서 상태 전이(04 §6). 검색 반영·삭제는 Job이 이어서 처리(웹은 직접 수집·삭제 안 함)."""
    from backend.admin.lifecycle import TRANSITIONS

    doc_id = _valid_id(document_id)
    rule = TRANSITIONS[action]
    if rule.job_action and not (settings.gcp_project_id and settings.ingestion_job_name):
        raise AppError("BAD_REQUEST", "문서 처리 Job 설정이 없습니다.")
    item, replay = await store.transition_document(
        doc_id,
        action,
        actor=actor,
        reason=body.reason,
        request_id=body.request_id.strip(),
        confirm_document_id=body.confirm_document_id,
    )
    if replay or not rule.job_action:
        return {**_document_view(item), "reused": replay}
    try:
        operation = await launcher.start_document(doc_id, rule.job_action)
    except Exception as exc:
        await store.mark_document_job(
            doc_id,
            action,
            actor=actor,
            request_id=body.request_id.strip(),
            operation_name=None,
            error_code="JOB_START_FAILED",
        )
        raise AppError("INTERNAL", "문서 처리 작업을 시작하지 못했습니다.") from exc
    item = await store.mark_document_job(
        doc_id, action, actor=actor, request_id=body.request_id.strip(), operation_name=operation
    )
    return {**_document_view(item), "reused": False}


@router.get("/places")
async def list_places(actor: Actor, store: Store, limit: Limit = 100, cursor: Cursor = None):
    del actor
    return await store.list_page("places", limit=limit, cursor=cursor, order_by="updated_at")


@router.post("/places", status_code=201)
async def create_place(
    body: PlaceCreate,
    actor: Actor,
    store: Store,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    changes = body.model_dump(exclude={"place_id", "reason", "request_id"}, exclude_none=True)
    return await store.save_place(
        body.place_id,
        changes,
        create=True,
        settings=settings,
        actor=actor,
        reason=body.reason.strip(),
        request_id=body.request_id.strip(),
    )


@router.get("/events")
async def list_events(actor: Actor, store: Store) -> dict[str, Any]:
    del actor
    page = await store.list_page(
        "campus_events", limit=300, cursor=None, order_by="end_date", descending=True
    )
    order = {"pending": 0, "active": 1, "disabled": 2}
    page["items"].sort(key=lambda r: order.get(str(r.get("status")), 3))
    return page


@router.patch("/events/{event_id}")
async def patch_event(
    event_id: str, body: EventPatch, actor: Actor, store: Store
) -> dict[str, Any]:
    return await store.set_event_status(
        _valid_id(event_id),
        body.status,
        actor=actor,
        reason=body.reason,
        request_id=body.request_id.strip(),
        start_date=body.start_date.isoformat() if body.start_date else None,
        end_date=body.end_date.isoformat() if body.end_date else None,
    )


@router.patch("/places/{place_id}")
async def patch_place(
    place_id: str,
    body: PlacePatch,
    actor: Actor,
    store: Store,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    changes = body.model_dump(exclude={"reason", "request_id"}, exclude_none=True)
    return await store.save_place(
        _valid_id(place_id),
        changes,
        create=False,
        settings=settings,
        actor=actor,
        reason=body.reason.strip(),
        request_id=body.request_id.strip(),
    )


@router.post("/directory", status_code=202)
async def upload_phonebook(
    actor: Actor,
    store: Store,
    launcher: Launcher,
    storage: Documents,
    settings: Annotated[Settings, Depends(get_settings)],
    file: Annotated[UploadFile, File()],
    reason: Annotated[str, Form(min_length=1, max_length=300)],
    request_id: Annotated[str, Form(min_length=8, max_length=100)],
) -> dict[str, Any]:
    """전화번호부(HWP 권장·PDF) 업로드 → Job이 변환·파싱해 부서 연락처를 통째로 교체(자동 적용)."""
    if not settings.rules_bucket or not settings.gcp_project_id or not settings.ingestion_job_name:
        raise AppError("BAD_REQUEST", "문서 저장소 또는 처리 Job 설정이 없습니다.")
    content = await file.read(20 * 1024 * 1024 + 1)
    document = validate_document(file.filename, content)
    if document.format not in ("hwp", "pdf"):
        raise AppError("BAD_REQUEST", "전화번호부는 HWP(권장) 또는 PDF로 올려 주세요.")
    doc_id = f"phonebook-{document.sha256.split(':')[1][:16]}"
    path = await storage.upload_staging(doc_id, document)
    await store.patch_source(
        "phonebook",
        {"kind": "phonebook", "status": "processing", "pending_file": path},
        actor=actor,
        reason=reason.strip(),
        request_id=request_id.strip(),
    )
    try:
        operation = await launcher.start_document(path, "phonebook")
    except Exception as exc:
        raise AppError("INTERNAL", "전화번호부 처리 작업을 시작하지 못했습니다.") from exc
    return {"status": "processing", "file": path, "operation": operation}


@router.get("/audit")
async def list_audit(
    actor: Actor,
    store: Store,
    limit: Limit = 50,
    cursor: Cursor = None,
) -> dict[str, Any]:
    del actor
    return await store.list_page("admin_audit", limit=limit, cursor=cursor, order_by="created_at")


@router.get("/versions")
async def list_versions(actor: Actor) -> dict[str, Any]:
    """버전·변경 내역(backend/app/version.py 단일 원본)."""
    del actor
    from backend.app.version import CHANGELOG, VERSION

    return {"current": VERSION, "items": CHANGELOG}


@router.get("/glossary")
async def get_glossary(actor: Actor) -> dict[str, Any]:
    del actor
    path = CONFIG_DIR / "glossary.yml"
    try:
        payload = path.read_bytes()
        data = yaml.safe_load(payload) or {}
        modified = path.stat().st_mtime_ns
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise AppError("INTERNAL", "용어 사전을 읽지 못했습니다.") from exc
    return {
        "version": data.get("version"),
        "content_sha256": hashlib.sha256(payload).hexdigest(),
        "applied_at_ns": modified,
        "glossary": data,
    }

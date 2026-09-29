"""Authenticated operational endpoints for CampusBridge administrators."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Annotated, Any

import yaml
from fastapi import APIRouter, Depends, Query

from backend.admin.auth import AdminActor, require_admin
from backend.admin.jobs import JobLauncher, get_job_launcher
from backend.admin.models import DisableRequest, IngestionRunRequest, SourcePatch
from backend.admin.store import AdminStore, get_admin_store
from backend.app.config import Settings, get_settings
from backend.domain import AppError

router = APIRouter(prefix="/api/admin", tags=["admin"])
Actor = Annotated[AdminActor, Depends(require_admin)]
Store = Annotated[AdminStore, Depends(get_admin_store)]
Launcher = Annotated[JobLauncher, Depends(get_job_launcher)]
Limit = Annotated[int, Query(ge=1, le=200)]
Cursor = Annotated[str | None, Query(max_length=200)]
CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def _valid_id(value: str) -> str:
    clean = value.strip()
    if not clean or len(clean) > 200 or "/" in clean or clean in {".", ".."}:
        raise AppError("BAD_REQUEST", "식별자 형식이 올바르지 않습니다.")
    return clean


@router.get("/sources")
async def list_sources(
    actor: Actor,
    store: Store,
    limit: Limit = 100,
    cursor: Cursor = None,
) -> dict[str, Any]:
    del actor
    return await store.list_page(
        "source_configs", limit=limit, cursor=cursor, order_by="kind", descending=False
    )


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


@router.get("/unanswered")
async def list_unanswered(
    actor: Actor,
    store: Store,
    limit: Limit = 50,
    cursor: Cursor = None,
) -> dict[str, Any]:
    del actor
    return await store.list_page(
        "unanswered", limit=limit, cursor=cursor, order_by="last_at"
    )


@router.get("/feedback")
async def list_feedback(
    actor: Actor,
    store: Store,
    limit: Limit = 50,
    cursor: Cursor = None,
) -> dict[str, Any]:
    del actor
    return await store.list_page(
        "feedback", limit=limit, cursor=cursor, order_by="created_at"
    )


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


@router.get("/audit")
async def list_audit(
    actor: Actor,
    store: Store,
    limit: Limit = 50,
    cursor: Cursor = None,
) -> dict[str, Any]:
    del actor
    return await store.list_page(
        "admin_audit", limit=limit, cursor=cursor, order_by="created_at"
    )


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

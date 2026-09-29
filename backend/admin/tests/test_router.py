from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.admin.auth import AdminActor, require_admin
from backend.admin.router import router
from backend.admin.store import get_admin_store
from backend.domain import AppError


class FakeStore:
    def __init__(self) -> None:
        self.patch_args = None
        self.disable_args = None

    async def get_document(self, collection: str, document_id: str):
        if collection == "ingestion_runs" and document_id == "run-1":
            return {
                "id": "run-1",
                "status": "running",
                "phase": "download",
                "processed": 3,
                "total": 10,
                "unsafe_log": "secret",
            }
        return None

    async def list_page(
        self,
        collection: str,
        *,
        limit: int,
        cursor: str | None,
        order_by: str,
        descending: bool = True,
    ) -> dict[str, Any]:
        return {
            "items": [{"id": f"{collection}-1", "order": order_by}],
            "next_cursor": cursor if limit == 1 else None,
        }

    async def patch_source(self, source_id: str, changes: dict[str, Any], **kwargs):
        self.patch_args = (source_id, changes, kwargs)
        return {"id": source_id, **changes}

    async def disable_documents(self, document_ids: list[str], **kwargs):
        self.disable_args = (document_ids, kwargs)
        return document_ids

    async def stats(self, days: int) -> dict[str, Any]:
        return {"days": days, "total": 7}


def _client(store: FakeStore) -> TestClient:
    app = FastAPI()

    @app.exception_handler(AppError)
    async def app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(exc.to_model().model_dump(), status_code=exc.status)

    app.include_router(router)
    app.dependency_overrides[require_admin] = lambda: AdminActor(
        email="admin@example.edu", subject="sub"
    )
    app.dependency_overrides[get_admin_store] = lambda: store
    return TestClient(app)


def test_admin_read_endpoints_apply_limits() -> None:
    client = _client(FakeStore())

    response = client.get("/api/admin/unanswered?limit=1")
    stats = client.get("/api/admin/stats?days=90")

    assert response.status_code == 200
    assert response.json()["items"][0]["id"] == "unanswered-1"
    assert stats.json() == {"days": 90, "total": 7}
    assert client.get("/api/admin/unanswered?limit=201").status_code == 422
    assert client.get("/api/admin/stats?days=91").status_code == 422


def test_source_patch_and_disable_forward_audited_context() -> None:
    store = FakeStore()
    client = _client(store)

    patched = client.patch(
        "/api/admin/sources/rules",
        json={"schedule": "daily", "reason": "운영 주기 변경", "request_id": "req-1"},
    )
    disabled = client.post(
        "/api/admin/disable",
        json={"document_ids": ["doc-1"], "reason": "오류 발견", "request_id": "req-2"},
    )

    assert patched.status_code == 200
    assert store.patch_args[0:2] == ("rules", {"schedule": "daily"})
    assert store.patch_args[2]["actor"].email == "admin@example.edu"
    assert disabled.status_code == 200
    assert store.disable_args[0] == ["doc-1"]
    assert disabled.json()["vertex_status"] == "pending"


def test_mutations_reject_missing_reason_or_invalid_identifier() -> None:
    client = _client(FakeStore())

    assert client.patch(
        "/api/admin/sources/rules", json={"schedule": "daily", "request_id": "req"}
    ).status_code == 422
    response = client.patch(
        "/api/admin/sources/bad%2Fid",
        json={"schedule": "daily", "reason": "test", "request_id": "req"},
    )
    assert response.status_code in {400, 404}


def test_ingestion_run_response_never_exposes_raw_log_fields() -> None:
    client = _client(FakeStore())

    response = client.get("/api/admin/ingestion-runs/run-1")

    assert response.status_code == 200
    assert response.json()["status"] == "running"
    assert "unsafe_log" not in response.json()

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.admin.auth import AdminActor, require_admin
from backend.admin.document_files import get_document_storage
from backend.admin.jobs import get_job_launcher
from backend.admin.router import router
from backend.admin.store import get_admin_store
from backend.app.config import Settings, get_settings
from backend.domain import AppError


class FakeStore:
    def __init__(self) -> None:
        self.patch_args = None
        self.disable_args = None
        self.create_result = (
            {
                "id": "run-created",
                "source_ids": ["notices"],
                "status": "queued",
                "phase": "dispatch",
            },
            True,
        )
        self.create_args = None
        self.mark_args = []
        self.admin_rows = [
            {
                "id": "dynamic@example.edu",
                "email": "dynamic@example.edu",
                "status": "active",
                "note": "운영",
            }
        ]
        self.admin_add_args = None
        self.admin_remove_args = None

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
        if collection == "documents" and document_id == "doc-ready":
            return {
                "id": "doc-ready",
                "title": "학생 안내",
                "status": "staging",
                "preview_status": "ready",
                "preview_chunks": [{"id": "c1", "text": "안내 내용"}],
                "gcs_staging_path": "secret/path",
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
        if collection == "admin_users":
            return {"items": self.admin_rows, "next_cursor": None}
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

    async def create_ingestion_run(self, source_ids: list[str], **kwargs):
        self.create_args = (source_ids, kwargs)
        return self.create_result

    async def mark_ingestion_launch(self, run_id: str, **kwargs):
        self.mark_args.append((run_id, kwargs))
        failed = kwargs.get("error_code") is not None
        return {
            "id": run_id,
            "source_ids": ["notices"],
            "status": "failed" if failed else "running",
            "phase": "dispatch" if failed else "starting",
            "error_code": kwargs.get("error_code"),
        }

    async def create_document(self, document_id: str, payload: dict[str, Any], **kwargs):
        self.document_create_args = (document_id, payload, kwargs)
        return {
            "id": document_id,
            **payload,
            "status": "staging",
            "preview_status": "pending",
        }, True

    async def mark_document_preview(self, document_id: str, **kwargs):
        self.document_mark_args = (document_id, kwargs)
        return {
            "id": document_id,
            "title": "학생 안내",
            "status": "staging",
            "preview_status": "failed" if kwargs.get("error_code") else "processing",
            "preview_error_code": kwargs.get("error_code"),
        }

    async def add_admin_user(self, email: str, **kwargs):
        self.admin_add_args = (email, kwargs)
        return {"id": email, "email": email, "status": "active", "note": kwargs["note"]}, False

    async def remove_admin_user(self, email: str, **kwargs):
        self.admin_remove_args = (email, kwargs)
        return {"id": email, "email": email, "status": "removed"}, False


class FakeLauncher:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = []

    async def start(self, run_id: str, source_ids: list[str]) -> str:
        self.calls.append((run_id, source_ids))
        if self.fail:
            raise RuntimeError("launch failed")
        return "operations/op-1"

    async def start_document(self, document_id: str, action: str) -> str:
        self.calls.append((document_id, action))
        if self.fail:
            raise RuntimeError("launch failed")
        return "operations/doc-op-1"


class FakeDocumentStorage:
    def __init__(self) -> None:
        self.calls = []

    async def upload_staging(self, document_id, document):
        self.calls.append((document_id, document))
        return f"documents/staging/{document_id}/original.txt"


def _client(
    store: FakeStore,
    *,
    launcher: FakeLauncher | None = None,
    document_storage: FakeDocumentStorage | None = None,
    settings: Settings | None = None,
) -> TestClient:
    app = FastAPI()

    @app.exception_handler(AppError)
    async def app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(exc.to_model().model_dump(), status_code=exc.status)

    app.include_router(router)
    app.dependency_overrides[require_admin] = lambda: AdminActor(
        email="admin@example.edu", subject="sub"
    )
    app.dependency_overrides[get_admin_store] = lambda: store
    app.dependency_overrides[get_job_launcher] = lambda: launcher or FakeLauncher()
    app.dependency_overrides[get_document_storage] = lambda: (
        document_storage or FakeDocumentStorage()
    )
    app.dependency_overrides[get_settings] = lambda: (
        settings
        or Settings(
            gcp_project_id="campus-bridge1",
            ingestion_job_name="campusbridge-ingest",
            rules_bucket="campusbridge-rules",
        )
    )
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


def test_admin_list_merges_bootstrap_and_firestore_users() -> None:
    store = FakeStore()
    client = _client(
        store,
        settings=Settings(admin_emails=frozenset({"bootstrap@example.edu"})),
    )

    response = client.get("/api/admin/admins")

    assert response.status_code == 200
    assert response.json()["current_email"] == "admin@example.edu"
    assert response.json()["items"][0] == {
        "email": "bootstrap@example.edu",
        "status": "active",
        "bootstrap": True,
    }
    assert response.json()["items"][1]["email"] == "dynamic@example.edu"
    assert response.json()["items"][1]["bootstrap"] is False


def test_admin_add_normalizes_email_and_forwards_audit_context() -> None:
    store = FakeStore()
    client = _client(store)

    response = client.post(
        "/api/admin/admins",
        json={
            "email": " New.Admin@Example.EDU ",
            "note": "산학협력단",
            "reason": "운영 담당 추가",
            "request_id": "request-add-1",
        },
    )

    assert response.status_code == 201
    assert store.admin_add_args[0] == "new.admin@example.edu"
    assert store.admin_add_args[1]["actor"].email == "admin@example.edu"
    assert store.admin_add_args[1]["reason"] == "운영 담당 추가"
    assert (
        client.post(
            "/api/admin/admins",
            json={
                "email": "not-an-email",
                "reason": "오류",
                "request_id": "request-add-2",
            },
        ).status_code
        == 422
    )


def test_admin_remove_rejects_bootstrap_and_self_then_records_reason() -> None:
    store = FakeStore()
    client = _client(
        store,
        settings=Settings(admin_emails=frozenset({"bootstrap@example.edu"})),
    )
    body = {"reason": "담당 변경", "request_id": "request-remove-1"}

    bootstrap = client.post("/api/admin/admins/bootstrap@example.edu/remove", json=body)
    assert bootstrap.status_code == 400
    assert client.post("/api/admin/admins/admin@example.edu/remove", json=body).status_code == 400
    response = client.post("/api/admin/admins/dynamic@example.edu/remove", json=body)

    assert response.status_code == 200
    assert store.admin_remove_args[0] == "dynamic@example.edu"
    assert store.admin_remove_args[1]["reason"] == "담당 변경"


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

    assert (
        client.patch(
            "/api/admin/sources/rules", json={"schedule": "daily", "request_id": "req"}
        ).status_code
        == 422
    )
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


def test_ingestion_run_starts_configured_job_and_records_launch() -> None:
    store = FakeStore()
    launcher = FakeLauncher()
    client = _client(store, launcher=launcher)

    response = client.post(
        "/api/admin/ingestion-runs",
        json={"source_ids": ["notices"], "idempotency_key": "request-123"},
    )

    assert response.status_code == 202
    assert response.json()["status"] == "running"
    assert response.json()["reused"] is False
    assert launcher.calls == [("run-created", ["notices"])]
    assert store.create_args[1]["actor"].email == "admin@example.edu"
    assert store.mark_args[0][1]["operation_name"] == "operations/op-1"


def test_ingestion_run_reuses_active_run_without_launching_again() -> None:
    store = FakeStore()
    store.create_result = (
        {"id": "run-existing", "status": "running", "phase": "download"},
        False,
    )
    launcher = FakeLauncher()
    client = _client(store, launcher=launcher)

    response = client.post(
        "/api/admin/ingestion-runs",
        json={"source_ids": [], "idempotency_key": "request-123"},
    )

    assert response.status_code == 202
    assert response.json()["reused"] is True
    assert response.json()["id"] == "run-existing"
    assert launcher.calls == []
    assert store.mark_args == []


def test_ingestion_run_rejects_missing_job_config_without_creating_record() -> None:
    store = FakeStore()
    client = _client(
        store,
        settings=Settings(gcp_project_id="campus-bridge1", ingestion_job_name=""),
    )

    response = client.post(
        "/api/admin/ingestion-runs",
        json={"source_ids": [], "idempotency_key": "request-123"},
    )

    assert response.status_code == 400
    assert store.create_args is None


def test_ingestion_run_records_dispatch_failure() -> None:
    store = FakeStore()
    client = _client(store, launcher=FakeLauncher(fail=True))

    response = client.post(
        "/api/admin/ingestion-runs",
        json={"source_ids": ["notices"], "idempotency_key": "request-123"},
    )

    assert response.status_code == 500
    assert store.mark_args[0][1]["error_code"] == "JOB_START_FAILED"


def test_document_upload_stages_file_and_dispatches_preview() -> None:
    store = FakeStore()
    launcher = FakeLauncher()
    storage = FakeDocumentStorage()
    client = _client(store, launcher=launcher, document_storage=storage)

    response = client.post(
        "/api/admin/documents",
        data={
            "title": "학생 안내",
            "department": "학사지원팀",
            "doc_date": "2026-09-29",
            "effective_from": "2026-10-01",
            "source": "교내 승인 문서",
            "approval_basis": "공개 답변 인용 승인 2026-09-29",
            "expires_at_doc": "2027-09-29",
            "reason": "신규 안내 반영",
            "request_id": "upload-12345",
        },
        files={"file": ("guide.txt", "안내 내용".encode(), "text/plain")},
    )

    assert response.status_code == 202
    assert response.json()["preview_status"] == "processing"
    document_id = response.json()["id"]
    assert storage.calls[0][0] == document_id
    assert launcher.calls == [(document_id, "preview")]
    assert store.document_create_args[1]["approval_basis"].startswith("공개 답변")


def test_document_upload_rejects_expiry_before_effective_date() -> None:
    store = FakeStore()
    storage = FakeDocumentStorage()
    client = _client(store, document_storage=storage)

    response = client.post(
        "/api/admin/documents",
        data={
            "title": "학생 안내",
            "department": "학사지원팀",
            "doc_date": "2026-09-29",
            "effective_from": "2027-10-01",
            "source": "교내 승인 문서",
            "approval_basis": "공개 답변 인용 승인",
            "expires_at_doc": "2027-09-29",
            "reason": "신규 안내 반영",
            "request_id": "upload-12345",
        },
        files={"file": ("guide.txt", b"text", "text/plain")},
    )

    assert response.status_code == 400
    assert storage.calls == []


def test_document_preview_exposes_only_safe_fields() -> None:
    response = _client(FakeStore()).get("/api/admin/documents/doc-ready/preview")

    assert response.status_code == 200
    assert response.json()["preview_chunks"][0]["text"] == "안내 내용"
    assert "gcs_staging_path" not in response.json()


def test_event_patch_forwards_audit_context_and_validates_status() -> None:
    store = FakeStore()
    seen: dict[str, Any] = {}

    async def set_event_status(event_id, status, *, actor, reason, request_id):
        seen.update(id=event_id, status=status, actor=actor.email, reason=reason, rid=request_id)
        return {"id": event_id, "status": status}

    store.set_event_status = set_event_status  # type: ignore[attr-defined]
    client = _client(store)
    body = {"status": "active", "reason": " 원문 확인 ", "request_id": "req-00000001"}
    assert client.patch("/api/admin/events/abc123", json=body).status_code == 200
    assert seen == {
        "id": "abc123",
        "status": "active",
        "actor": "admin@example.edu",
        "reason": "원문 확인",
        "rid": "req-00000001",
    }
    bad = client.patch("/api/admin/events/abc123", json={**body, "status": "verified"})
    assert bad.status_code == 422
    listed = client.get("/api/admin/events").json()
    assert listed["items"][0]["id"] == "campus_events-1"

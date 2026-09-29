from __future__ import annotations

from typing import Any

import pytest

from backend.admin.lifecycle import check_transition
from backend.admin.tests.test_router import FakeLauncher, FakeStore, _client
from backend.domain import AppError

READY = {"status": "staging", "preview_status": "ready", "approval_basis": "학생처 공문 2026-1"}


def check(action: str, current: dict[str, Any], **kw):
    return check_transition(action, current, request_id="req-00000001", document_id="doc-1", **kw)


def test_publish_requires_preview_and_approval_basis():
    rule, replay = check("publish", READY)
    assert rule.to_status == "publishing" and rule.job_action == "publish" and not replay
    with pytest.raises(AppError, match="미리보기"):
        check("publish", {**READY, "preview_status": "processing"})
    with pytest.raises(AppError, match="승인 근거"):
        check("publish", {**READY, "approval_basis": "  "})


def test_invalid_state_transitions_are_rejected():
    with pytest.raises(AppError, match="현재 상태"):
        check("archive", READY)  # staging은 archive 불가
    with pytest.raises(AppError, match="현재 상태"):
        check("reject", {"status": "published"})
    with pytest.raises(AppError, match="지원하지 않는"):
        check("delete", READY)


def test_archive_and_purge_disable_search_and_purge_needs_confirmation():
    rule, _ = check("archive", {"status": "published"})
    assert rule.disable and rule.to_status == "archived"
    with pytest.raises(AppError, match="확인 문구"):
        check("purge", {"status": "archived"})
    rule, _ = check("purge", {"status": "archived"}, confirm_document_id="doc-1")
    assert rule.disable and rule.job_action == "purge"


def test_same_request_replay_is_idempotent():
    _, replay = check(
        "publish", {**READY, "status": "publishing", "last_request_id": "req-00000001"}
    )
    assert replay


class LifecycleStore(FakeStore):
    def __init__(self, current: dict[str, Any]) -> None:
        super().__init__()
        self.current = current
        self.transitions: list[tuple[str, str]] = []
        self.jobs: list[tuple[str, str | None]] = []

    async def transition_document(self, document_id, action, **kw):
        rule, replay = check_transition(
            action,
            self.current,
            document_id=document_id,
            request_id=kw["request_id"],
            confirm_document_id=kw.get("confirm_document_id"),
        )
        if not replay:
            self.current = {
                **self.current,
                "status": rule.to_status,
                "last_request_id": kw["request_id"],
            }
            self.transitions.append((document_id, action))
        return {"id": document_id, **self.current}, replay

    async def mark_document_job(self, document_id, action, **kw):
        self.jobs.append((action, kw.get("error_code")))
        return {"id": document_id, **self.current, "job_error_code": kw.get("error_code")}


BODY = {"reason": "학생처 승인 확인", "request_id": "req-00000001"}


def test_publish_route_transitions_and_launches_job():
    store, launcher = LifecycleStore(dict(READY)), FakeLauncher()
    c = _client(store, launcher=launcher)
    r = c.post("/api/admin/documents/doc-1/publish", json=BODY)
    assert r.status_code == 200 and r.json()["status"] == "publishing"
    assert launcher.calls == [("doc-1", "publish")] and store.jobs == [("publish", None)]
    # 같은 요청 재전송 → Job 재실행 없음
    r2 = c.post("/api/admin/documents/doc-1/publish", json=BODY)
    assert r2.json()["reused"] is True and len(launcher.calls) == 1


def test_reject_needs_no_job_and_bad_action_is_422():
    store, launcher = LifecycleStore(dict(READY)), FakeLauncher()
    c = _client(store, launcher=launcher)
    assert c.post("/api/admin/documents/doc-1/reject", json=BODY).json()["status"] == "rejected"
    assert launcher.calls == []
    assert c.post("/api/admin/documents/doc-1/destroy", json=BODY).status_code == 422


def test_purge_without_confirmation_is_400_and_job_failure_is_recorded():
    store = LifecycleStore({"status": "archived"})
    c = _client(store, launcher=FakeLauncher(fail=True))
    r = c.post("/api/admin/documents/doc-1/purge", json=BODY)
    assert r.status_code == 400 and store.transitions == []
    r = c.post("/api/admin/documents/doc-1/purge", json={**BODY, "confirm_document_id": "doc-1"})
    assert r.status_code == 500 and store.jobs == [("purge", "JOB_START_FAILED")]

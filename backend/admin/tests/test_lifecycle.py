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
        self.denylisted = False

    async def transition_document(self, document_id, action, **kw):
        rule, replay = check_transition(
            action,
            self.current,
            document_id=document_id,
            request_id=kw["request_id"],
            confirm_document_id=kw.get("confirm_document_id"),
        )
        if not replay:
            previous_status = self.current["status"]
            self.current = {
                **self.current,
                "status": rule.to_status,
                "last_request_id": kw["request_id"],
                "last_action": action,
                "dispatch_from_status": previous_status,
                "job_operation_name": None,
                "job_error_code": None,
            }
            if rule.disable:
                self.denylisted = True
            self.transitions.append((document_id, action))
        return {"id": document_id, **self.current}, replay

    async def mark_document_job(self, document_id, action, **kw):
        self.jobs.append((action, kw.get("error_code")))
        self.current = {
            **self.current,
            "job_operation_name": kw.get("operation_name"),
            "job_error_code": kw.get("error_code"),
        }
        if kw.get("error_code"):
            self.current["status"] = self.current["dispatch_from_status"]
        return {"id": document_id, **self.current}


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


def test_purge_without_confirmation_is_400():
    store = LifecycleStore({"status": "archived"})
    c = _client(store, launcher=FakeLauncher(fail=True))
    r = c.post("/api/admin/documents/doc-1/purge", json=BODY)
    assert r.status_code == 400 and store.transitions == []


@pytest.mark.parametrize(
    ("action", "initial", "body", "transitional"),
    [
        ("publish", dict(READY), BODY, "publishing"),
        ("archive", {"status": "published"}, BODY, "archived"),
        (
            "purge",
            {"status": "archived"},
            {**BODY, "confirm_document_id": "doc-1"},
            "purging",
        ),
    ],
)
def test_document_dispatch_failure_can_retry_same_request(
    action: str,
    initial: dict[str, Any],
    body: dict[str, Any],
    transitional: str,
):
    store, launcher = LifecycleStore(initial), FakeLauncher(fail=True)
    c = _client(store, launcher=launcher)

    first = c.post(f"/api/admin/documents/doc-1/{action}", json=body)
    assert first.status_code == 500
    assert store.current["status"] == initial["status"]
    assert store.jobs == [(action, "JOB_START_FAILED")]
    if action in {"archive", "purge"}:
        assert store.denylisted  # 실패 복구 뒤에도 긴급 회수는 유지한다.

    launcher.fail = False
    retried = c.post(f"/api/admin/documents/doc-1/{action}", json=body)
    assert retried.status_code == 200
    assert retried.json()["status"] == transitional
    assert retried.json()["reused"] is False
    assert launcher.calls == [("doc-1", action), ("doc-1", action)]
    assert store.jobs[-1] == (action, None)

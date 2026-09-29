"""Firestore queries and audited mutations used by the admin router."""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import uuid4

from backend.admin.auth import AdminActor
from backend.app.config import Settings, get_settings
from backend.domain import AppError


def _safe_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _safe_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_value(item) for item in value]
    return value


class AdminStore(Protocol):
    async def get_document(self, collection: str, document_id: str) -> dict[str, Any] | None: ...

    async def list_page(
        self,
        collection: str,
        *,
        limit: int,
        cursor: str | None,
        order_by: str,
        descending: bool = True,
    ) -> dict[str, Any]: ...

    async def patch_source(
        self,
        source_id: str,
        changes: dict[str, Any],
        *,
        actor: AdminActor,
        reason: str,
        request_id: str,
    ) -> dict[str, Any]: ...

    async def disable_documents(
        self,
        document_ids: list[str],
        *,
        actor: AdminActor,
        reason: str,
        request_id: str,
    ) -> list[str]: ...

    async def stats(self, days: int) -> dict[str, Any]: ...

    async def create_ingestion_run(
        self,
        source_ids: list[str],
        *,
        idempotency_key: str,
        actor: AdminActor,
    ) -> tuple[dict[str, Any], bool]: ...

    async def mark_ingestion_launch(
        self,
        run_id: str,
        *,
        actor: AdminActor,
        operation_name: str | None,
        error_code: str | None = None,
    ) -> dict[str, Any]: ...

    async def create_document(
        self,
        document_id: str,
        payload: dict[str, Any],
        *,
        actor: AdminActor,
        reason: str,
        request_id: str,
    ) -> tuple[dict[str, Any], bool]: ...

    async def mark_document_preview(
        self,
        document_id: str,
        *,
        actor: AdminActor,
        request_id: str,
        operation_name: str | None,
        error_code: str | None = None,
    ) -> dict[str, Any]: ...

    async def transition_document(
        self,
        document_id: str,
        action: str,
        *,
        actor: AdminActor,
        reason: str,
        request_id: str,
        confirm_document_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]: ...

    async def mark_document_job(
        self,
        document_id: str,
        action: str,
        *,
        actor: AdminActor,
        request_id: str,
        operation_name: str | None,
        error_code: str | None = None,
    ) -> dict[str, Any]: ...

    async def save_place(
        self,
        place_id: str,
        changes: dict[str, Any],
        *,
        create: bool,
        settings: Settings,
        actor: AdminActor,
        reason: str,
        request_id: str,
    ) -> dict[str, Any]: ...

    async def add_admin_user(
        self,
        email: str,
        *,
        note: str,
        actor: AdminActor,
        reason: str,
        request_id: str,
    ) -> tuple[dict[str, Any], bool]: ...

    async def set_event_status(
        self,
        event_id: str,
        status: str,
        *,
        actor: AdminActor,
        reason: str,
        request_id: str,
    ) -> dict[str, Any]: ...

    async def remove_admin_user(
        self,
        email: str,
        *,
        actor: AdminActor,
        reason: str,
        request_id: str,
    ) -> tuple[dict[str, Any], bool]: ...


class FirestoreAdminStore:
    def __init__(self, settings: Settings | None = None, client: Any = None):
        from google.cloud import firestore

        self._fs = firestore
        self.settings = settings or get_settings()
        self.db = client or firestore.AsyncClient(
            project=self.settings.gcp_project_id or None,
            database=self.settings.firestore_db,
        )

    async def get_document(self, collection: str, document_id: str) -> dict[str, Any] | None:
        snapshot = await self.db.collection(collection).document(document_id).get()
        if not snapshot.exists:
            return None
        return {"id": snapshot.id, **_safe_value(snapshot.to_dict() or {})}

    async def list_page(
        self,
        collection: str,
        *,
        limit: int,
        cursor: str | None,
        order_by: str,
        descending: bool = True,
    ) -> dict[str, Any]:
        direction = self._fs.Query.DESCENDING if descending else self._fs.Query.ASCENDING
        query = self.db.collection(collection).order_by(order_by, direction=direction)
        if cursor:
            snapshot = await self.db.collection(collection).document(cursor).get()
            if snapshot.exists:
                query = query.start_after(snapshot)
        snapshots = [snapshot async for snapshot in query.limit(limit + 1).stream()]
        has_more = len(snapshots) > limit
        snapshots = snapshots[:limit]
        items = [{"id": item.id, **_safe_value(item.to_dict() or {})} for item in snapshots]
        return {
            "items": items,
            "next_cursor": snapshots[-1].id if has_more and snapshots else None,
        }

    def _audit_payload(
        self,
        *,
        actor: AdminActor,
        action: str,
        target: str,
        before: Any,
        after: Any,
        reason: str,
        request_id: str,
        result: str = "success",
    ) -> dict[str, Any]:
        return {
            "actor": actor.email,
            "actor_sub": actor.subject,
            "action": action,
            "target": target,
            "before": before,
            "after": after,
            "reason": reason,
            "request_id": request_id,
            "result": result,
            "created_at": datetime.now(UTC),
        }

    async def add_admin_user(
        self,
        email: str,
        *,
        note: str,
        actor: AdminActor,
        reason: str,
        request_id: str,
    ) -> tuple[dict[str, Any], bool]:
        ref = self.db.collection("admin_users").document(email)
        request_ref = self.db.collection("admin_requests").document(
            hashlib.sha256(request_id.encode()).hexdigest()
        )

        @self._fs.async_transactional
        async def txn(tx) -> tuple[dict[str, Any], bool]:
            request_snapshot = await request_ref.get(transaction=tx)
            if request_snapshot.exists:
                request = request_snapshot.to_dict() or {}
                if request.get("action") != "admin.add" or request.get("target") != email:
                    raise AppError(
                        "BAD_REQUEST", "요청 ID가 다른 관리자 변경에 이미 사용되었습니다."
                    )
                current = await ref.get(transaction=tx)
                row = current.to_dict() or {} if current.exists else {}
                return {"id": email, **_safe_value(row)}, True

            snapshot = await ref.get(transaction=tx)
            before = snapshot.to_dict() if snapshot.exists else None
            if before and before.get("status") == "active":
                raise AppError("BAD_REQUEST", "이미 등록된 관리자입니다.")
            now = datetime.now(UTC)
            after = {
                "email": email,
                "added_by": actor.email,
                "added_at": now,
                "note": note,
                "status": "active",
            }
            tx.set(ref, after)
            tx.set(
                request_ref,
                {
                    "action": "admin.add",
                    "target": email,
                    "request_id": request_id,
                    "created_at": now,
                },
            )
            tx.set(
                self.db.collection("admin_audit").document(str(uuid4())),
                self._audit_payload(
                    actor=actor,
                    action="admin.add",
                    target=email,
                    before=before,
                    after=after,
                    reason=reason,
                    request_id=request_id,
                ),
            )
            return {"id": email, **_safe_value(after)}, False

        return await txn(self.db.transaction())

    async def remove_admin_user(
        self,
        email: str,
        *,
        actor: AdminActor,
        reason: str,
        request_id: str,
    ) -> tuple[dict[str, Any], bool]:
        ref = self.db.collection("admin_users").document(email)
        request_ref = self.db.collection("admin_requests").document(
            hashlib.sha256(request_id.encode()).hexdigest()
        )

        @self._fs.async_transactional
        async def txn(tx) -> tuple[dict[str, Any], bool]:
            request_snapshot = await request_ref.get(transaction=tx)
            if request_snapshot.exists:
                request = request_snapshot.to_dict() or {}
                if request.get("action") != "admin.remove" or request.get("target") != email:
                    raise AppError(
                        "BAD_REQUEST", "요청 ID가 다른 관리자 변경에 이미 사용되었습니다."
                    )
                current = await ref.get(transaction=tx)
                row = current.to_dict() or {} if current.exists else {}
                return {"id": email, **_safe_value(row)}, True

            snapshot = await ref.get(transaction=tx)
            before = snapshot.to_dict() if snapshot.exists else None
            if not before or before.get("status") != "active":
                raise AppError("BAD_REQUEST", "활성 관리자 계정을 찾지 못했습니다.")
            now = datetime.now(UTC)
            changes = {
                "status": "removed",
                "removed_by": actor.email,
                "removed_at": now,
                "removal_reason": reason,
            }
            after = {**before, **changes}
            tx.set(ref, changes, merge=True)
            tx.set(
                request_ref,
                {
                    "action": "admin.remove",
                    "target": email,
                    "request_id": request_id,
                    "created_at": now,
                },
            )
            tx.set(
                self.db.collection("admin_audit").document(str(uuid4())),
                self._audit_payload(
                    actor=actor,
                    action="admin.remove",
                    target=email,
                    before=before,
                    after=after,
                    reason=reason,
                    request_id=request_id,
                ),
            )
            return {"id": email, **_safe_value(after)}, False

        return await txn(self.db.transaction())

    async def patch_source(
        self,
        source_id: str,
        changes: dict[str, Any],
        *,
        actor: AdminActor,
        reason: str,
        request_id: str,
    ) -> dict[str, Any]:
        ref = self.db.collection("source_configs").document(source_id)
        snapshot = await ref.get()
        if not snapshot.exists:
            raise AppError("BAD_REQUEST", "등록된 출처를 찾지 못했습니다.")
        before = snapshot.to_dict() or {}
        after = {**before, **changes, "updated_at": datetime.now(UTC)}
        audit_ref = self.db.collection("admin_audit").document(str(uuid4()))
        batch = self.db.batch()
        batch.set(ref, after, merge=True)
        batch.set(
            audit_ref,
            self._audit_payload(
                actor=actor,
                action="source.update",
                target=source_id,
                before=before,
                after=after,
                reason=reason,
                request_id=request_id,
            ),
        )
        await batch.commit()
        return {"id": source_id, **_safe_value(after)}

    async def disable_documents(
        self,
        document_ids: list[str],
        *,
        actor: AdminActor,
        reason: str,
        request_id: str,
    ) -> list[str]:
        now = datetime.now(UTC)
        batch = self.db.batch()
        for document_id in document_ids:
            ref = self.db.collection("disabled_document_ids").document(document_id)
            snapshot = await ref.get()
            before = snapshot.to_dict() if snapshot.exists else None
            after = {
                "reason": reason,
                "actor": actor.email,
                "created_at": now,
                "vertex_status": "pending",
            }
            audit_ref = self.db.collection("admin_audit").document(str(uuid4()))
            batch.set(ref, after)
            batch.set(
                audit_ref,
                self._audit_payload(
                    actor=actor,
                    action="document.disable",
                    target=document_id,
                    before=before,
                    after=after,
                    reason=reason,
                    request_id=request_id,
                ),
            )
        await batch.commit()
        return document_ids

    async def stats(self, days: int) -> dict[str, Any]:
        since = datetime.now(UTC) - timedelta(days=days)
        query = self.db.collection("turns").where("created_at", ">=", since)
        outcomes: Counter[str] = Counter()
        daily: Counter[str] = Counter()
        elapsed: list[int] = []
        async for snapshot in query.stream():
            row = snapshot.to_dict() or {}
            created = row.get("created_at")
            if isinstance(created, datetime):
                daily[created.date().isoformat()] += 1
            outcomes[str(row.get("outcome") or "unknown")] += 1
            if isinstance(row.get("elapsed_ms"), int):
                elapsed.append(row["elapsed_ms"])
        elapsed.sort()
        return {
            "since": since.isoformat(),
            "days": days,
            "total": sum(outcomes.values()),
            "daily": dict(sorted(daily.items())),
            "outcomes": dict(outcomes),
            "latency_ms": {
                "average": round(sum(elapsed) / len(elapsed), 1) if elapsed else None,
                "p95": elapsed[max(0, int(len(elapsed) * 0.95) - 1)] if elapsed else None,
            },
        }

    async def create_ingestion_run(
        self,
        source_ids: list[str],
        *,
        idempotency_key: str,
        actor: AdminActor,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically deduplicate both a request key and an already active run."""
        run_id = f"run-{hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]}"
        run_ref = self.db.collection("ingestion_runs").document(run_id)
        control_ref = self.db.collection("ingestion_control").document("active")

        @self._fs.async_transactional
        async def txn(tx) -> tuple[dict[str, Any], bool]:
            requested = await run_ref.get(transaction=tx)
            if requested.exists:
                return {"id": requested.id, **_safe_value(requested.to_dict() or {})}, False

            control = await control_ref.get(transaction=tx)
            active_id = (control.to_dict() or {}).get("run_id") if control.exists else None
            if isinstance(active_id, str) and active_id:
                active_ref = self.db.collection("ingestion_runs").document(active_id)
                active = await active_ref.get(transaction=tx)
                active_data = active.to_dict() or {} if active.exists else {}
                if active_data.get("status") in {"queued", "running"}:
                    return {"id": active.id, **_safe_value(active_data)}, False

            now = datetime.now(UTC)
            row = {
                "idempotency_key": idempotency_key,
                "source_ids": source_ids,
                "status": "queued",
                "phase": "dispatch",
                "processed": 0,
                "total": 0,
                "added": 0,
                "changed": 0,
                "rejected": 0,
                "error_code": None,
                "actor": actor.email,
                "created_at": now,
                "started_at": None,
                "finished_at": None,
            }
            audit_ref = self.db.collection("admin_audit").document(str(uuid4()))
            tx.set(run_ref, row)
            tx.set(control_ref, {"run_id": run_id, "updated_at": now})
            tx.set(
                audit_ref,
                self._audit_payload(
                    actor=actor,
                    action="ingestion.request",
                    target=run_id,
                    before=None,
                    after=row,
                    reason="관리자 강제 재수집",
                    request_id=idempotency_key,
                    result="queued",
                ),
            )
            return {"id": run_id, **_safe_value(row)}, True

        return await txn(self.db.transaction())

    async def mark_ingestion_launch(
        self,
        run_id: str,
        *,
        actor: AdminActor,
        operation_name: str | None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        ref = self.db.collection("ingestion_runs").document(run_id)
        snapshot = await ref.get()
        if not snapshot.exists:
            raise AppError("INTERNAL", "수집 실행 기록이 사라졌습니다.")
        before = snapshot.to_dict() or {}
        now = datetime.now(UTC)
        failed = error_code is not None
        changes = {
            "status": "failed" if failed else "running",
            "phase": "dispatch" if failed else "starting",
            "operation_name": operation_name,
            "error_code": error_code,
            "started_at": before.get("started_at") or now,
            "finished_at": now if failed else None,
            "updated_at": now,
        }
        after = {**before, **changes}
        audit_ref = self.db.collection("admin_audit").document(str(uuid4()))
        batch = self.db.batch()
        batch.set(ref, changes, merge=True)
        if failed:
            control_ref = self.db.collection("ingestion_control").document("active")
            batch.set(control_ref, {"run_id": None, "updated_at": now}, merge=True)
        batch.set(
            audit_ref,
            self._audit_payload(
                actor=actor,
                action="ingestion.launch",
                target=run_id,
                before=before,
                after=after,
                reason="Cloud Run Job 실행",
                request_id=str(before.get("idempotency_key") or run_id),
                result="failed" if failed else "success",
            ),
        )
        await batch.commit()
        return {"id": run_id, **_safe_value(after)}

    async def create_document(
        self,
        document_id: str,
        payload: dict[str, Any],
        *,
        actor: AdminActor,
        reason: str,
        request_id: str,
    ) -> tuple[dict[str, Any], bool]:
        ref = self.db.collection("documents").document(document_id)

        @self._fs.async_transactional
        async def txn(tx) -> tuple[dict[str, Any], bool]:
            snapshot = await ref.get(transaction=tx)
            if snapshot.exists:
                current = snapshot.to_dict() or {}
                if current.get("request_id") != request_id or current.get("sha256") != payload.get(
                    "sha256"
                ):
                    raise AppError("BAD_REQUEST", "요청 ID가 다른 문서에 이미 사용되었습니다.")
                return {"id": snapshot.id, **_safe_value(current)}, False
            now = datetime.now(UTC)
            row = {
                **payload,
                "status": "staging",
                "preview_status": "pending",
                "version": 1,
                "previous_version_id": None,
                "uploader": actor.email,
                "request_id": request_id,
                "created_at": now,
                "updated_at": now,
            }
            audit_ref = self.db.collection("admin_audit").document(str(uuid4()))
            tx.set(ref, row)
            tx.set(
                audit_ref,
                self._audit_payload(
                    actor=actor,
                    action="document.upload",
                    target=document_id,
                    before=None,
                    after=row,
                    reason=reason,
                    request_id=request_id,
                ),
            )
            return {"id": document_id, **_safe_value(row)}, True

        return await txn(self.db.transaction())

    async def mark_document_preview(
        self,
        document_id: str,
        *,
        actor: AdminActor,
        request_id: str,
        operation_name: str | None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        ref = self.db.collection("documents").document(document_id)
        snapshot = await ref.get()
        if not snapshot.exists:
            raise AppError("INTERNAL", "업로드한 문서 기록이 사라졌습니다.")
        before = snapshot.to_dict() or {}
        now = datetime.now(UTC)
        changes = {
            "preview_status": "failed" if error_code else "processing",
            "preview_operation_name": operation_name,
            "preview_error_code": error_code,
            "updated_at": now,
        }
        after = {**before, **changes}
        batch = self.db.batch()
        batch.set(ref, changes, merge=True)
        batch.set(
            self.db.collection("admin_audit").document(str(uuid4())),
            self._audit_payload(
                actor=actor,
                action="document.preview.dispatch",
                target=document_id,
                before=before,
                after=after,
                reason="미리보기 변환 작업 시작",
                request_id=request_id,
                result="failed" if error_code else "success",
            ),
        )
        await batch.commit()
        return {"id": document_id, **_safe_value(after)}

    async def transition_document(
        self,
        document_id: str,
        action: str,
        *,
        actor: AdminActor,
        reason: str,
        request_id: str,
        confirm_document_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        from backend.admin.lifecycle import check_transition

        ref = self.db.collection("documents").document(document_id)
        deny_ref = self.db.collection("disabled_document_ids").document(document_id)

        @self._fs.async_transactional
        async def txn(tx) -> tuple[dict[str, Any], bool]:
            snapshot = await ref.get(transaction=tx)
            if not snapshot.exists:
                raise AppError("BAD_REQUEST", "문서를 찾지 못했습니다.")
            before = snapshot.to_dict() or {}
            rule, replay = check_transition(
                action,
                before,
                request_id=request_id,
                confirm_document_id=confirm_document_id,
                document_id=document_id,
            )
            if replay:
                return {"id": document_id, **_safe_value(before)}, True
            now = datetime.now(UTC)
            changes = {
                "status": rule.to_status,
                "last_action": action,
                "last_request_id": request_id,
                "updated_at": now,
                f"{action}_by": actor.email,
                f"{action}_at": now,
            }
            if rule.job_action:
                changes.update(
                    {
                        "dispatch_from_status": before.get("status"),
                        "job_operation_name": None,
                        "job_error_code": None,
                        "job_dispatch_status": "pending",
                    }
                )
            if rule.disable:
                tx.set(
                    deny_ref,
                    {
                        "reason": reason,
                        "actor": actor.email,
                        "created_at": now,
                        "source": f"document.{action}",
                    },
                )
            after = {**before, **changes}
            tx.set(ref, changes, merge=True)
            tx.set(
                self.db.collection("admin_audit").document(str(uuid4())),
                self._audit_payload(
                    actor=actor,
                    action=f"document.{action}",
                    target=document_id,
                    before=before,
                    after=after,
                    reason=reason,
                    request_id=request_id,
                ),
            )
            return {"id": document_id, **_safe_value(after)}, False

        return await txn(self.db.transaction())

    async def mark_document_job(
        self,
        document_id: str,
        action: str,
        *,
        actor: AdminActor,
        request_id: str,
        operation_name: str | None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        ref = self.db.collection("documents").document(document_id)

        @self._fs.async_transactional
        async def txn(tx) -> dict[str, Any]:
            snapshot = await ref.get(transaction=tx)
            if not snapshot.exists:
                raise AppError("BAD_REQUEST", "문서를 찾지 못했습니다.")
            before = snapshot.to_dict() or {}
            now = datetime.now(UTC)
            changes: dict[str, Any] = {
                "job_action": action,
                "job_operation_name": operation_name,
                "job_error_code": error_code,
                "job_dispatch_status": "failed" if error_code else "started",
                "updated_at": now,
            }
            # 디스패치 실패는 같은 request_id로 다시 시도할 수 있도록 직전 안정
            # 상태로 원자적으로 되돌린다. archive/purge denylist는 별도 문서이므로
            # 의도적으로 유지해 검색 노출이 다시 열리지 않게 한다.
            if (
                error_code
                and before.get("last_request_id") == request_id
                and before.get("last_action") == action
                and not before.get("job_operation_name")
                and before.get("dispatch_from_status")
            ):
                changes["status"] = before["dispatch_from_status"]
            after = {**before, **changes}
            tx.set(ref, changes, merge=True)
            tx.set(
                self.db.collection("admin_audit").document(str(uuid4())),
                self._audit_payload(
                    actor=actor,
                    action=f"document.{action}.dispatch",
                    target=document_id,
                    before=before,
                    after=after,
                    reason=f"{action} 작업 시작",
                    request_id=request_id,
                    result="failed" if error_code else "success",
                ),
            )
            return {"id": document_id, **_safe_value(after)}

        return await txn(self.db.transaction())

    async def save_place(
        self,
        place_id: str,
        changes: dict[str, Any],
        *,
        create: bool,
        settings: Settings,
        actor: AdminActor,
        reason: str,
        request_id: str,
    ) -> dict[str, Any]:
        from backend.admin.places import normalize_place_changes

        ref = self.db.collection("places").document(place_id)

        @self._fs.async_transactional
        async def txn(tx) -> dict[str, Any]:
            snapshot = await ref.get(transaction=tx)
            current = snapshot.to_dict() if snapshot.exists else None
            if create and current is not None:
                if current.get("request_id") == request_id:
                    return {"id": place_id, **_safe_value(current)}  # 재전송 멱등
                raise AppError("BAD_REQUEST", "같은 place_id가 이미 있습니다.")
            if not create and current is None:
                raise AppError("BAD_REQUEST", "장소를 찾지 못했습니다.")
            out = normalize_place_changes(changes, current, settings)
            now = datetime.now(UTC)
            out.update({"updated_at": now, "updated_by": actor.email, "request_id": request_id})
            if create:
                out.update({"place_id": place_id, "created_at": now})
            if out.get("status") == "verified":
                out.update({"verified_by": actor.email, "verified_at": now})
            after = {**(current or {}), **out}
            tx.set(ref, out, merge=True)
            tx.set(
                self.db.collection("admin_audit").document(str(uuid4())),
                self._audit_payload(
                    actor=actor,
                    action="place.create" if create else "place.update",
                    target=place_id,
                    before=current,
                    after=after,
                    reason=reason,
                    request_id=request_id,
                ),
            )
            return {"id": place_id, **_safe_value(after)}

        return await txn(self.db.transaction())


    async def set_event_status(
        self,
        event_id: str,
        status: str,
        *,
        actor: AdminActor,
        reason: str,
        request_id: str,
    ) -> dict[str, Any]:
        """일정 게시(active)·숨김(disabled) + 감사 로그(같은 트랜잭션).

        재수집은 status를 덮어쓰지 않는다.
        """
        ref = self.db.collection("campus_events").document(event_id)

        @self._fs.async_transactional
        async def txn(tx) -> dict[str, Any]:
            snapshot = await ref.get(transaction=tx)
            if not snapshot.exists:
                raise AppError("BAD_REQUEST", "일정을 찾지 못했습니다.")
            current = snapshot.to_dict() or {}
            if current.get("request_id") == request_id:
                return {"id": event_id, **_safe_value(current)}  # 재전송 멱등
            now = datetime.now(UTC)
            out = {
                "status": status,
                "reviewed_by": actor.email,
                "reviewed_at": now,
                "request_id": request_id,
                "updated_at": now,
            }
            after = {**current, **out}
            tx.set(ref, out, merge=True)
            tx.set(
                self.db.collection("admin_audit").document(str(uuid4())),
                self._audit_payload(
                    actor=actor,
                    action=f"event.{status}",
                    target=event_id,
                    before=current,
                    after=after,
                    reason=reason,
                    request_id=request_id,
                ),
            )
            return {"id": event_id, **_safe_value(after)}

        return await txn(self.db.transaction())


def get_admin_store() -> AdminStore:
    return FirestoreAdminStore()

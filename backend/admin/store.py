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
        direction = (
            self._fs.Query.DESCENDING if descending else self._fs.Query.ASCENDING
        )
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


def get_admin_store() -> AdminStore:
    return FirestoreAdminStore()

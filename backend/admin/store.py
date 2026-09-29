"""Firestore queries and audited mutations used by the admin router."""

from __future__ import annotations

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


def get_admin_store() -> AdminStore:
    return FirestoreAdminStore()

"""Firestore 저장소(상세설계 04 §2-1, 01 §5) — 이름 있는 DB, 서버 서비스 계정만 접근.

멱등 확인+잠금, 완료 기록은 각각 하나의 트랜잭션이다. 판정은 base의 순수 함수를 쓴다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from backend.app.config import Settings, get_settings
from backend.store.base import (
    LOG_TTL,
    Acquire,
    Completion,
    apply_completion,
    decide_acquire,
    unanswered_id,
)


class FirestoreStore:
    def __init__(self, settings: Settings | None = None, client: Any = None):
        from google.cloud import firestore  # 지연 import — 테스트에 SDK 불필요

        s = settings or get_settings()
        self._fs = firestore
        self.db = client or firestore.AsyncClient(
            project=s.gcp_project_id or None, database=s.firestore_db
        )

    def _refs(self, thread_id: str, request_id: str):
        return (
            self.db.collection("threads").document(thread_id),
            self.db.collection("turns").document(f"{thread_id}_{request_id}"),
        )

    async def acquire(self, thread_id: str, request_id: str, query_masked: str) -> Acquire:
        thread_ref, turn_ref = self._refs(thread_id, request_id)

        @self._fs.async_transactional
        async def txn(tx) -> Acquire:
            turn_snap = await turn_ref.get(transaction=tx)
            thread_snap = await thread_ref.get(transaction=tx)
            result, turn, lock = decide_acquire(
                turn_snap.to_dict() if turn_snap.exists else None,
                thread_snap.to_dict() if thread_snap.exists else None,
                request_id,
                datetime.now(UTC),
            )
            if turn is not None:
                tx.set(
                    turn_ref,
                    {
                        **turn,
                        "thread_id": thread_id,
                        "request_id": request_id,
                        "query_masked": query_masked,
                    },
                    merge=True,
                )
            if lock is not None:
                tx.set(thread_ref, lock, merge=True)
            return result

        return await txn(self.db.transaction())

    async def complete(self, thread_id: str, request_id: str, c: Completion) -> None:
        thread_ref, turn_ref = self._refs(thread_id, request_id)
        un_ref = (
            self.db.collection("unanswered").document(unanswered_id(c.unanswered["query_masked"]))
            if c.unanswered
            else None
        )

        @self._fs.async_transactional
        async def txn(tx) -> None:
            turn_snap = await turn_ref.get(transaction=tx)
            un_snap = await un_ref.get(transaction=tx) if un_ref is not None else None
            now = datetime.now(UTC)
            turn_up, thread_up, count = apply_completion(
                turn_snap.to_dict() if turn_snap.exists else None, c, now
            )
            if turn_up is None:
                return
            tx.set(turn_ref, turn_up, merge=True)
            tx.set(thread_ref, thread_up, merge=True)
            if count and un_ref is not None and c.unanswered:
                prev = un_snap.to_dict() if un_snap is not None and un_snap.exists else {}
                tx.set(
                    un_ref,
                    {
                        **c.unanswered,
                        "count": prev.get("count", 0) + 1,
                        "first_at": prev.get("first_at", now),
                        "last_at": now,
                        "expires_at": now + LOG_TTL,
                    },
                )

        await txn(self.db.transaction())

    async def release(self, thread_id: str, request_id: str, *, failed: bool) -> None:
        thread_ref, turn_ref = self._refs(thread_id, request_id)

        @self._fs.async_transactional
        async def txn(tx) -> None:
            thread_snap = await thread_ref.get(transaction=tx)
            turn_snap = await turn_ref.get(transaction=tx)
            t = thread_snap.to_dict() if thread_snap.exists else {}
            if t.get("lock_owner") == request_id:
                tx.set(thread_ref, {"lock_owner": None, "processing_until": None}, merge=True)
            u = turn_snap.to_dict() if turn_snap.exists else None
            if failed and u and u.get("status") != "done":
                tx.set(turn_ref, {"status": "failed"}, merge=True)

        await txn(self.db.transaction())

    async def put_feedback(self, doc: dict[str, Any]) -> None:
        now = datetime.now(UTC)
        await (
            self.db.collection("feedback")
            .document(doc["turn_id"])
            .set({**doc, "created_at": now, "expires_at": now + LOG_TTL})
        )

    async def add_event(self, doc: dict[str, Any]) -> None:
        now = datetime.now(UTC)
        await self.db.collection("events").add(
            {**doc, "created_at": now, "expires_at": now + LOG_TTL}
        )

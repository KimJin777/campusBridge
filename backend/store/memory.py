"""메모리 저장소 — 로컬 개발·테스트용. 판정은 base의 순수 함수를 그대로 쓴다."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from backend.store.base import (
    LOG_TTL,
    Acquire,
    Completion,
    apply_completion,
    decide_acquire,
    unanswered_id,
)


class MemoryStore:
    def __init__(self, clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.clock = clock
        self.threads: dict[str, dict[str, Any]] = {}
        self.turns: dict[str, dict[str, Any]] = {}
        self.unanswered: dict[str, dict[str, Any]] = {}
        self.feedback: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()  # Firestore 트랜잭션의 직렬화를 흉내 낸다

    async def acquire(self, thread_id: str, request_id: str, query_masked: str) -> Acquire:
        async with self._lock:
            tid = f"{thread_id}_{request_id}"
            now = self.clock()
            result, turn, lock = decide_acquire(
                self.turns.get(tid), self.threads.get(thread_id), request_id, now
            )
            if turn is not None:
                self.turns[tid] = {
                    **self.turns.get(tid, {}),
                    **turn,
                    "thread_id": thread_id,
                    "request_id": request_id,
                    "query_masked": query_masked,
                }
            if lock is not None:
                self.threads.setdefault(thread_id, {}).update(lock)
            return result

    async def complete(self, thread_id: str, request_id: str, c: Completion) -> None:
        async with self._lock:
            tid = f"{thread_id}_{request_id}"
            now = self.clock()
            turn_up, thread_up, count = apply_completion(self.turns.get(tid), c, now)
            if turn_up is None:
                return
            self.turns.setdefault(tid, {}).update(turn_up)
            self.threads.setdefault(thread_id, {}).update(thread_up or {})
            if count and c.unanswered:
                uid = unanswered_id(c.unanswered["query_masked"])
                u = self.unanswered.get(uid) or {"count": 0, "first_at": now}
                u.update({**c.unanswered, "count": u["count"] + 1, "last_at": now})
                u["expires_at"] = now + LOG_TTL
                self.unanswered[uid] = u

    async def release(self, thread_id: str, request_id: str, *, failed: bool) -> None:
        async with self._lock:
            t = self.threads.get(thread_id)
            if t and t.get("lock_owner") == request_id:
                t["lock_owner"] = None
                t["processing_until"] = None
            turn = self.turns.get(f"{thread_id}_{request_id}")
            if failed and turn and turn.get("status") != "done":
                turn["status"] = "failed"

    async def put_feedback(self, doc: dict[str, Any]) -> None:
        now = self.clock()
        self.feedback[doc["turn_id"]] = {**doc, "created_at": now, "expires_at": now + LOG_TTL}

    async def get_unanswered(self, uid: str) -> dict[str, Any] | None:
        return self.unanswered.get(uid)

    async def update_unanswered(self, uid: str, fields: dict[str, Any]) -> None:
        self.unanswered[uid].update(fields)

    async def add_event(self, doc: dict[str, Any]) -> None:
        now = self.clock()
        self.events.append({**doc, "created_at": now, "expires_at": now + LOG_TTL})

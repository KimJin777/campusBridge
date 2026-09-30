"""턴 멱등·대화 잠금·완료 트랜잭션 계약(상세설계 04 §2-1, 01 §5).

판정 로직(decide_acquire·apply_completion)은 순수 함수로 두고, 저장소 구현(메모리·Firestore)은
이 함수를 각자의 트랜잭션 안에서 호출한다 → 두 구현의 동작이 같다.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

from backend.domain.thread import ThreadDoc

LEASE = timedelta(seconds=30)
THREAD_TTL = timedelta(hours=24)
LOG_TTL = timedelta(days=90)
UNANSWERED_REASONS = {"no_evidence", "verification_failed"}


@dataclass
class Acquire:
    kind: Literal["new", "replay", "busy"]
    thread: ThreadDoc = field(default_factory=ThreadDoc)
    final_payload: dict[str, Any] | None = None
    attempt: int = 0


@dataclass
class Completion:
    """완료 트랜잭션 입력 — 그래프 결과로 API 계층이 만든다."""

    outcome: Literal["answer", "fallback", "ask"]
    final_payload: dict[str, Any]
    turn_fields: dict[str, Any]  # intent, retrieved_ids, cited_ids, fallback_reason, elapsed_ms 등
    thread_update: dict[str, Any]  # save_state 노드 산출 + profile·last_intent
    unanswered: dict[str, Any] | None = (
        None  # {query_masked, intent, fallback_reason, suggested_dept_id}
    )


def unanswered_id(query_masked: str) -> str:
    norm = " ".join(query_masked.lower().split())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


def decide_acquire(
    turn: dict[str, Any] | None,
    thread: dict[str, Any] | None,
    request_id: str,
    now: datetime,
) -> tuple[Acquire, dict[str, Any] | None, dict[str, Any] | None]:
    """(판정, 쓸 turn 문서, 쓸 thread 잠금 필드). busy·replay면 쓰지 않는다."""
    thread_doc = ThreadDoc.model_validate(_thread_fields(thread)) if thread else ThreadDoc()
    if thread_doc.expires_at and thread_doc.expires_at <= now:
        thread_doc = ThreadDoc()  # TTL 삭제 전이라도 만료된 대화는 새 대화로 본다
    if turn and turn.get("status") == "done":
        return (
            Acquire("replay", thread_doc, turn.get("final_payload"), turn.get("attempt", 1)),
            None,
            None,
        )
    if (
        turn
        and turn.get("status") == "processing"
        and turn.get("lease_until")
        and turn["lease_until"] > now
    ):
        return Acquire("busy"), None, None
    if (
        thread
        and thread.get("processing_until")
        and thread["processing_until"] > now
        and thread.get("lock_owner") != request_id
    ):
        return Acquire("busy"), None, None
    attempt = (turn or {}).get("attempt", 0) + 1
    new_turn = {
        "status": "processing",
        "lease_until": now + LEASE,
        "attempt": attempt,
        "created_at": (turn or {}).get("created_at", now),
        "expires_at": (turn or {}).get("created_at", now) + LOG_TTL,
    }
    lock = {"lock_owner": request_id, "processing_until": now + LEASE}
    return Acquire("new", thread_doc, None, attempt), new_turn, lock


def apply_completion(
    turn: dict[str, Any] | None, c: Completion, now: datetime
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, bool]:
    """(turn 갱신, thread 갱신, unanswered 반영 여부). 이미 done이면 아무것도 다시 쓰지 않는다."""
    if turn and turn.get("side_effects_done"):
        return None, None, False
    turn_update = {
        **c.turn_fields,
        "status": "done",
        "outcome": c.outcome,
        "final_payload": c.final_payload,
        "side_effects_done": True,
    }
    thread_update = {
        **_dump_thread_update(c.thread_update),
        "updated_at": now,
        "expires_at": now + THREAD_TTL,
    }
    count_unanswered = (
        bool(c.unanswered) and c.unanswered.get("fallback_reason") in UNANSWERED_REASONS
    )
    return turn_update, thread_update, count_unanswered


def _dump_thread_update(update: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in update.items():
        out[k] = v.model_dump(mode="python", by_alias=True) if hasattr(v, "model_dump") else v
    return out


_THREAD_KEYS = set(ThreadDoc.model_fields)


def _thread_fields(doc: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in doc.items() if k in _THREAD_KEYS and v is not None}


class TurnStore(Protocol):
    async def acquire(self, thread_id: str, request_id: str, query_masked: str) -> Acquire: ...

    async def complete(self, thread_id: str, request_id: str, c: Completion) -> None: ...

    async def release(self, thread_id: str, request_id: str, *, failed: bool) -> None: ...

    async def put_feedback(self, doc: dict[str, Any]) -> None: ...

    async def add_event(self, doc: dict[str, Any]) -> None: ...

    # 미응답 재확인(교수님 #765)
    async def get_unanswered(self, uid: str) -> dict[str, Any] | None: ...

    async def update_unanswered(self, uid: str, fields: dict[str, Any]) -> None: ...

import asyncio
from datetime import UTC, datetime, timedelta

from backend.domain import LastTurn, PendingQuestion, Profile
from backend.store.base import Completion, unanswered_id
from backend.store.memory import MemoryStore


class Clock:
    def __init__(self):
        self.t = datetime(2026, 9, 29, 3, 0, tzinfo=UTC)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


def run(c):
    return asyncio.run(c)


def completion(outcome="answer", reason=None, **thread_update):
    return Completion(
        outcome=outcome,
        final_payload={"answer": {"sentences": []}},
        turn_fields={"intent": "procedure", "fallback_reason": reason},
        thread_update=thread_update or {"profile": Profile(grade=2), "pending_question": None},
        unanswered={"query_masked": "휴학 연장  가능?", "fallback_reason": reason}
        if reason
        else None,
    )


def test_new_then_replay_after_done():
    s = MemoryStore(Clock())
    a = run(s.acquire("t1", "r1", "휴학"))
    assert a.kind == "new" and a.attempt == 1
    run(s.complete("t1", "r1", completion()))
    run(s.release("t1", "r1", failed=False))
    b = run(s.acquire("t1", "r1", "휴학"))
    assert b.kind == "replay" and b.final_payload == {"answer": {"sentences": []}}
    assert b.thread.profile.grade == 2


def test_same_request_processing_is_busy_until_lease_expires():
    clock = Clock()
    s = MemoryStore(clock)
    run(s.acquire("t1", "r1", "q"))
    assert run(s.acquire("t1", "r1", "q")).kind == "busy"
    clock.advance(seconds=31)  # 프로세스가 죽어 임대 만료 → 회수
    again = run(s.acquire("t1", "r1", "q"))
    assert again.kind == "new" and again.attempt == 2


def test_other_request_same_thread_is_busy_then_free_after_release():
    s = MemoryStore(Clock())
    run(s.acquire("t1", "r1", "q"))
    assert run(s.acquire("t1", "r2", "q")).kind == "busy"
    run(s.release("t1", "r1", failed=True))
    assert s.turns["t1_r1"]["status"] == "failed"
    assert run(s.acquire("t1", "r2", "q")).kind == "new"


def test_failed_turn_can_retry_with_same_request_id():
    s = MemoryStore(Clock())
    run(s.acquire("t1", "r1", "q"))
    run(s.release("t1", "r1", failed=True))
    a = run(s.acquire("t1", "r1", "q"))
    assert a.kind == "new" and a.attempt == 2


def test_completion_is_idempotent_and_counts_unanswered_once():
    s = MemoryStore(Clock())
    run(s.acquire("t1", "r1", "q"))
    c = completion("fallback", "no_evidence")
    run(s.complete("t1", "r1", c))
    run(s.complete("t1", "r1", c))
    u = s.unanswered[unanswered_id("휴학 연장 가능?")]
    assert u["count"] == 1 and u["expires_at"] > u["last_at"]


def test_out_of_scope_not_counted_as_unanswered():
    s = MemoryStore(Clock())
    run(s.acquire("t1", "r1", "q"))
    run(s.complete("t1", "r1", completion("fallback", "out_of_scope")))
    assert s.unanswered == {}


def test_thread_state_persists_pending_and_last_turn():
    clock = Clock()
    s = MemoryStore(clock)
    pq = PendingQuestion(
        text="학년?", original_query_masked="휴학", missing_slots=["grade"], asked_at=clock()
    )
    run(s.acquire("t1", "r1", "휴학"))
    run(s.complete("t1", "r1", completion("ask", pending_question=pq, clarification_count=1)))
    run(s.release("t1", "r1", failed=False))
    a = run(s.acquire("t1", "r2", "2학년"))
    assert a.thread.pending_question.original_query_masked == "휴학"
    assert a.thread.clarification_count == 1
    lt = LastTurn(query_masked="휴학", answer_summary="…", cited_ids=["x"], topic="휴학")
    run(
        s.complete(
            "t1", "r2", completion(last_turn=lt, pending_question=None, clarification_count=0)
        )
    )
    run(s.release("t1", "r2", failed=False))
    b = run(s.acquire("t1", "r3", "그건요?"))
    assert b.thread.last_turn.topic == "휴학" and b.thread.pending_question is None
    assert s.threads["t1"]["expires_at"] == clock() + timedelta(hours=24)


def test_expired_thread_starts_fresh():
    clock = Clock()
    s = MemoryStore(clock)
    run(s.acquire("t1", "r1", "q"))
    run(s.complete("t1", "r1", completion(profile=Profile(grade=3))))
    run(s.release("t1", "r1", failed=False))
    clock.advance(hours=25)
    assert run(s.acquire("t1", "r2", "q")).thread.profile == Profile()


def test_feedback_overwrites_by_turn_id():
    s = MemoryStore(Clock())
    run(s.put_feedback({"turn_id": "t1_r1", "thread_id": "t1", "rating": 1}))
    run(s.put_feedback({"turn_id": "t1_r1", "thread_id": "t1", "rating": -1}))
    assert len(s.feedback) == 1 and s.feedback["t1_r1"]["rating"] == -1

import json
import uuid

from fastapi.testclient import TestClient

from backend.agent.llm import LLMUnavailable
from backend.app.config import Settings
from backend.app.main import RateLimiter, create_app
from backend.store.memory import MemoryStore
from backend.tests.test_graph import GOOD_DRAFT, FakeLLM, deps, leave_classify


def parse(text):
    events = []
    for block in text.strip().split("\n\n"):
        if block.startswith(":"):
            continue
        lines = dict(line.split(": ", 1) for line in block.split("\n"))
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def client(llm, store=None):
    store = store or MemoryStore()
    app = create_app(Settings(), deps=deps(llm), store=store)
    return TestClient(app), store


def body(**kw):
    b = {
        "schema_version": 1,
        "thread_id": str(uuid.uuid4()),
        "request_id": str(uuid.uuid4()),
        "message": "휴학하려면 어떻게 해요? 학번 20231234",
    }
    b.update(kw)
    return b


def test_chat_ask_then_answer_across_turns():
    llm = FakeLLM(
        classify=leave_classify(needed_slots=["grade", "scholarship"]), compose=GOOD_DRAFT
    )
    c, store = client(llm)
    b = body()
    r = c.post("/api/chat", json=b)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    ev = parse(r.text)
    assert [e for e, _ in ev][0] == "meta" and ev[-1][0] == "done" and ev[-1][1]["outcome"] == "ask"
    turn = store.turns[f"{b['thread_id']}_{b['request_id']}"]
    assert "20231234" not in turn["query_masked"]  # 마스킹 후 저장
    assert store.threads[b["thread_id"]]["pending_question"]["original_query_masked"]

    llm.out["classify"] = leave_classify(
        answers_pending=True, extracted_profile={"grade": 2, "scholarship": "no"}
    )
    r2 = c.post("/api/chat", json=body(thread_id=b["thread_id"], message="2학년, 장학금 없어요"))
    names = [e for e, _ in parse(r2.text)]
    assert names[0] == "meta" and "evidence" in names and names[-2:] == ["answer", "done"]
    assert store.threads[b["thread_id"]]["pending_question"] is None
    assert store.threads[b["thread_id"]]["lock_owner"] is None


def test_replay_same_request_id_without_model_calls():
    llm = FakeLLM(classify=leave_classify(), compose=GOOD_DRAFT)
    c, store = client(llm)
    b = body()
    store.threads[b["thread_id"]] = {"profile": {"grade": 2, "scholarship": "no"}}
    c.post("/api/chat", json=b)
    n = len(llm.calls)
    ev = parse(c.post("/api/chat", json=b).text)
    assert ev[0] == ("meta", ev[0][1]) and ev[0][1]["replay"] is True
    assert [e for e, _ in ev] == ["meta", "evidence", "answer", "done"]
    assert all(card["state"] == "cited" for card in ev[1][1]["items"])
    assert len(llm.calls) == n


def test_busy_thread_returns_409():
    c, store = client(FakeLLM(classify=leave_classify(), compose=GOOD_DRAFT))
    b = body()
    import asyncio

    asyncio.run(store.acquire(b["thread_id"], "other", "q"))
    r = c.post("/api/chat", json=b)
    assert r.status_code == 409 and r.json()["code"] == "THREAD_BUSY"


def test_llm_error_emits_error_then_done_and_marks_failed():
    c, store = client(FakeLLM(fail={"classify": LLMUnavailable("down")}))
    b = body()
    ev = parse(c.post("/api/chat", json=b).text)
    assert [e for e, _ in ev][-2:] == ["error", "done"]
    assert ev[-2][1]["code"] == "LLM_UNAVAILABLE" and ev[-1][1]["outcome"] == "error"
    assert store.turns[f"{b['thread_id']}_{b['request_id']}"]["status"] == "failed"


def test_bad_requests():
    c, _ = client(FakeLLM())
    assert c.post("/api/chat", json=body(schema_version=2)).json()["code"] == "SCHEMA_MISMATCH"
    assert c.post("/api/chat", json=body(thread_id="not-uuid")).status_code == 400
    assert c.post("/api/chat", json=body(message="   ")).status_code == 400
    assert c.post("/api/chat", json=body(message="x" * 501)).status_code == 400


def test_feedback_track_status_and_headers():
    c, store = client(FakeLLM())
    tid = str(uuid.uuid4())
    r = c.post(
        "/api/feedback",
        json={
            "thread_id": tid,
            "turn_id": f"{tid}_x",
            "rating": -1,
            "comment": "010-1234-5678로 연락",
        },
    )
    assert r.status_code == 204 and "[전화]" in store.feedback[f"{tid}_x"]["comment_masked"]
    assert (
        c.post("/api/feedback", json={"thread_id": tid, "turn_id": "a", "rating": 0}).status_code
        == 400
    )
    assert c.post("/api/track", json={"thread_id": tid, "event": "new_thread"}).status_code == 204
    s = c.get("/api/status")
    assert s.json()["schema_version"] == 1 and s.headers["cache-control"] == "no-store"
    assert c.get("/api/status?deep=1").status_code == 401


def test_rate_limiter():
    rl = RateLimiter(per_minute=2)
    assert rl.allow("1.1.1.1", 0) and rl.allow("1.1.1.1", 1) and not rl.allow("1.1.1.1", 2)
    assert rl.allow("1.1.1.1", 61.5)

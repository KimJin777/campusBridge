"""밤사이 자동 처리 집계(교수님 2026-10-01)."""

from backend.app.auto_triage import run_feedback, run_reports, run_unanswered, summary_line


async def test_counts_and_summary_line():
    async def recheck(uid):
        if uid == "bad":
            raise RuntimeError("boom")
        return {"status": "verified" if uid == "a" else "unresolved"}

    u = await run_unanswered(["a", "b", "bad"], recheck)
    assert u == {"verified": 1, "still_unanswered": 1, "error": 1}

    steps = {
        "r1": {"action": "verify", "status": "verified"},
        "r2": {"action": "analyze", "status": "proposed"},
        "r3": {"action": "skip", "needs_human": True},
        "r4": {"action": "wait", "reason": "재수집 중"},
    }

    async def step(rid):
        return steps[rid]

    r = await run_reports(list(steps), step)
    assert r == {"resolved": 1, "analyzed": 1, "needs_approval": 1, "waiting": 1}

    marked = {}

    async def query_of(tid):
        return None if tid == "t2" else "휴학 신청 언제까지야?"

    async def run_turn(q):
        return {"outcome": "fallback", "fallback_reason": "no_evidence"}

    async def mark(fid, fields):
        marked[fid] = fields["recheck"]["result"]

    f = await run_feedback(
        [("t1", {"turn_id": "t1"}), ("t2", {"turn_id": "t2"})], query_of, run_turn, mark
    )
    assert f == {"still_fails": 1, "no_question": 1} and marked == {
        "t1": "still_fails",
        "t2": "no_question",
    }

    line = summary_line({"unanswered": dict(u), "reports": dict(r), "feedback": dict(f)})
    assert "미응답 검증완료 1/3" in line and "승인 대기 1" in line and "제보 자동 종결 1" in line

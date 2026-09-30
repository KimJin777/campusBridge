import asyncio
import time

from backend.agent.graph import build_graph
from backend.agent.llm import LLMTimeout, LLMUnavailable, StructuredOutputError
from backend.agent.nodes import AgentDeps
from backend.agent.state import ActCall, ActOut, ClassifyOut
from backend.domain import (
    Dept,
    Draft,
    DraftSentence,
    Evidence,
    PendingQuestion,
    Profile,
    ToolResult,
)

ART = Evidence(
    id="101_main_32",
    kind="article",
    title="학칙 제32조(휴학)",
    text="휴학은 통산 3년을 초과할 수 없다. 휴학은 학기 개시일로부터 30일 이내에 신청하여야 한다.",
    meta={"dept_id": "acad"},
)
GUIDE = Evidence(
    id="guide:4390:1",
    kind="guide",
    title="휴학 안내 — 신청",
    text="휴학 신청은 학생포털에서 한다. 학사지원팀이 승인한다.",
    meta={"dept_id": "acad"},
)
CAL = Evidence(
    id="cal:2026-09-01:0",
    kind="calendar",
    title="2026-2학기 휴학 신청 기간",
    text="2026-2학기 일반휴학 신청: 9월 1일 ~ 9월 30일",
)
DEPT = Dept(dept_id="acad", name="학사지원팀", phone="055-000-0000")


class FakeLLM:
    def __init__(self, classify=None, compose=None, act=None, fail=None):
        self.out = {"classify": classify, "compose": compose, "act": act}
        self.fail = fail or {}
        self.calls = []

    async def structured(self, schema, system, user, *, node, deadline):
        self.calls.append(node)
        f = self.fail.get(node)
        if f:
            err = f.pop(0) if isinstance(f, list) else f
            if err:
                raise err
        return self.out[node]


def tool(result, log=None, name=None):
    async def fn(**args):
        if log is not None:
            log.append((name, args))
        return result

    return fn


def leave_classify(**kw):
    base = dict(
        intent="procedure",
        in_scope=True,
        topic="휴학",
        search_query="휴학 신청 휴학기간",
        normalized_query="휴학하려면 어떻게 해요",
        needed_slots=[],
        evidence_needs=["procedure_and_contact", "current_deadline"],
    )
    base.update(kw)
    return ClassifyOut(**base)


GOOD_DRAFT = Draft(
    sentences=[
        DraftSentence(
            text="휴학은 학기 개시일로부터 30일 이내에 신청해야 합니다.",
            cite_ids=["101_main_32"],
            supporting_quotes=["학기 개시일로부터 30일 이내에 신청하여야 한다"],
        )
    ],
    checklist=[
        DraftSentence(
            text="학생포털에서 휴학 신청",
            cite_ids=["guide:4390:1"],
            supporting_quotes=["휴학 신청은 학생포털에서 한다"],
        )
    ],
)


def deps(llm, tools=None, **kw):
    t = {
        "search_academic_knowledge": tool(ToolResult(ok=True, items=[GUIDE, ART])),
        "get_academic_calendar": tool(ToolResult(ok=True, items=[CAL])),
        "get_notices": tool(ToolResult.empty()),
        "get_menu": tool(ToolResult.empty()),
        "find_campus_location": tool(ToolResult.empty()),
    }
    t.update(tools or {})
    return AgentDeps(llm=llm, tools=t, dept_lookup=lambda i: DEPT if i == "acad" else None, **kw)


def state(query="휴학하려면 어떻게 해요", **kw):
    now = time.monotonic()
    s = {
        "thread_id": "t",
        "request_id": "r",
        "query": query,
        "started": now,
        "deadline": now + 25,
        "profile": Profile(grade=2, scholarship="no"),
        "pending_question": None,
        "clarification_count": 0,
        "last_turn": None,
        "evidence": [],
        "tool_calls_count": 0,
        "llm_calls_count": 0,
        "tool_failures": [],
        "review_flags": [],
        "act_used": False,
        "pending_tool_calls": [],
    }
    s.update(kw)
    return s


def run(d, s):
    return asyncio.run(build_graph(d).ainvoke(s))


def test_leave_scenario_answers_with_two_llm_calls():
    llm = FakeLLM(classify=leave_classify(), compose=GOOD_DRAFT)
    out = run(deps(llm), state())
    assert out["outcome"] == "answer"
    assert llm.calls == ["classify", "compose"]  # 대표 경로 모델 호출 2회
    assert out["tool_calls_count"] == 3
    a = out["answer"]
    assert a.cited == ["101_main_32", "guide:4390:1"]
    assert a.dept.name == "학사지원팀"
    assert out["last_turn"].topic == "휴학" and out["pending_question"] is None


def test_missing_slots_asks_once_and_saves_pending():
    grad = leave_classify(topic="졸업", normalized_query="졸업하려면 어떻게 해요")
    st = state(query="졸업하려면 어떻게 해요", profile=Profile())
    out = run(deps(FakeLLM(classify=grad)), st)
    assert out["outcome"] == "ask"
    assert out["ask"]["missing_slots"] == ["dept"]
    assert out["pending_question"].original_query_masked == "졸업하려면 어떻게 해요"
    assert out["clarification_count"] == 1


def test_leave_procedure_answers_without_asking_grade():
    """휴학 절차는 학년·장학과 무관 — 되묻지 않고 바로 답한다(20문항 #13·14)."""
    c = leave_classify(needed_slots=["grade", "scholarship"])
    out = run(deps(FakeLLM(classify=c, compose=GOOD_DRAFT)), state(profile=Profile()))
    assert out["outcome"] == "answer"


def test_pending_answer_restores_original_query_and_merges_profile():
    pq = PendingQuestion(
        text="학년…",
        original_query_masked="이번 학기 휴학 언제까지",
        missing_slots=["grade"],
        asked_at="2026-09-29T00:00:00Z",
    )
    c = leave_classify(
        answers_pending=True,
        extracted_profile=Profile(grade=2, scholarship="yes"),
        needed_slots=["grade", "scholarship"],
        search_query="",
    )
    log = []
    llm = FakeLLM(classify=c, compose=GOOD_DRAFT)
    d = deps(
        llm,
        {"search_academic_knowledge": tool(ToolResult(ok=True, items=[GUIDE, ART]), log, "search")},
    )
    out = run(
        d,
        state(
            query="2학년이고 장학금 받아요",
            pending_question=pq,
            clarification_count=1,
            profile=Profile(),
        ),
    )
    assert out["effective_query"] == "이번 학기 휴학 언제까지"
    assert log[0][1]["query"] == "이번 학기 휴학 언제까지"
    assert out["profile"].grade == 2 and out["outcome"] == "answer"
    assert out["pending_question"] is None and out["clarification_count"] == 0


def test_still_missing_after_one_ask_answers_generally():
    llm = FakeLLM(classify=leave_classify(needed_slots=["grade"]), compose=GOOD_DRAFT)
    out = run(deps(llm), state(clarification_count=1, profile=Profile()))
    assert out["outcome"] == "answer"


def test_out_of_scope_fallback_without_tools():
    llm = FakeLLM(classify=ClassifyOut(intent="rule", in_scope=False))
    out = run(deps(llm), state(query="오늘 주식 뭐 사요"))
    assert out["outcome"] == "fallback" and out["fallback"].reason == "out_of_scope"
    assert llm.calls == ["classify"]


def test_all_tools_fail_is_tool_failure():
    fail = tool(ToolResult.fail("UPSTREAM_TIMEOUT"))
    llm = FakeLLM(classify=leave_classify(), compose=GOOD_DRAFT, act=ActOut())
    d = deps(
        llm, {"search_academic_knowledge": fail, "get_academic_calendar": fail, "get_notices": fail}
    )
    out = run(d, state())
    assert out["outcome"] == "fallback" and out["fallback_reason"] == "tool_failure"
    assert "compose" not in llm.calls


def test_unverifiable_draft_falls_back():
    bad = Draft(
        sentences=[
            DraftSentence(
                text="휴학은 5년까지 됩니다.",
                cite_ids=["101_main_32"],
                supporting_quotes=["통산 5년까지 가능"],
            )
        ]
    )
    out = run(deps(FakeLLM(classify=leave_classify(), compose=bad)), state())
    assert out["fallback_reason"] == "verification_failed"


def test_act_supplements_missing_need_once():
    log = []
    c = leave_classify(evidence_needs=["eligibility_or_limit", "current_deadline"])
    act = ActOut(
        calls=[
            ActCall(name="get_notices", args={"board": "academic", "keyword": "휴학 연장"}),
            ActCall(name="get_academic_calendar", args={}),
            ActCall(name="delete_everything", args={}),
        ]
    )
    llm = FakeLLM(classify=c, compose=GOOD_DRAFT, act=act)

    async def notices(**args):
        log.append(("notices", args))
        return (
            ToolResult(ok=True, items=[CAL])
            if args["keyword"] == "휴학 연장"
            else ToolResult.empty()
        )

    d = deps(
        llm,
        {
            "get_academic_calendar": tool(ToolResult.fail("UPSTREAM_TIMEOUT")),
            "get_notices": notices,
        },
    )
    out = run(d, state())
    assert llm.calls == ["classify", "act", "compose"]
    assert ("notices", {"board": "academic", "keyword": "휴학 연장"}) in log
    assert out["tool_calls_count"] == 4  # 계획 3 + 보충 1(실패 원천·허용 밖 도구 제외)
    assert out["outcome"] == "answer"


def test_classify_retry_then_error():
    llm = FakeLLM(
        classify=leave_classify(),
        fail={"classify": [StructuredOutputError("x"), StructuredOutputError("y")]},
    )
    out = run(deps(llm), state())
    assert out["outcome"] == "error" and llm.calls == ["classify", "classify"]
    assert "last_turn" not in out or out["last_turn"] is None


def test_llm_unavailable_is_error_and_keeps_pending():
    pq = PendingQuestion(
        text="학년?",
        original_query_masked="휴학",
        missing_slots=["grade"],
        asked_at="2026-09-29T00:00:00Z",
    )
    llm = FakeLLM(fail={"classify": LLMUnavailable("down")})
    out = run(deps(llm), state(pending_question=pq, clarification_count=1))
    assert out["outcome"] == "error" and out["error_code"] == "LLM_UNAVAILABLE"
    assert out["pending_question"] == pq and out["clarification_count"] == 1


def test_confusable_correction_is_blocked_and_asked():
    c = leave_classify(
        corrections=[{"from": "퇴학", "to": "휴학", "confidence": 0.9, "kind": "spelling"}]
    )
    out = run(
        deps(FakeLLM(classify=c), glossary_confusables=[{"휴학", "퇴학", "자퇴", "제적"}]),
        state(query="퇴학 신청"),
    )
    assert out["outcome"] == "ask"
    assert out["corrections"] == [] and "휴학" in out["clarification_candidates"]
    assert "말씀하신 건가요" in out["ask"]["question"]


def test_deadline_near_skips_tools_and_compose():
    now = time.monotonic()
    out = run(deps(FakeLLM(classify=leave_classify(), compose=GOOD_DRAFT)), state(deadline=now + 3))
    assert out["tool_calls_count"] == 0
    assert out["fallback_reason"] == "deadline"


def test_stream_custom_events_order():
    llm = FakeLLM(classify=leave_classify(), compose=GOOD_DRAFT)

    async def go():
        events = []
        async for chunk in build_graph(deps(llm)).astream(state(), stream_mode="custom"):
            events.append(chunk["event"])
        return events

    events = asyncio.run(go())
    assert events[0] == "status" and "evidence" in events
    assert events.index("evidence") < events.index("answer")
    assert events[-1] == "answer"


def test_compose_timeout_is_deadline_fallback_not_error():
    llm = FakeLLM(classify=leave_classify(), fail={"compose": LLMTimeout("slow")})
    out = run(deps(llm), state())
    assert out["outcome"] == "fallback" and out["fallback_reason"] == "deadline"


def test_location_fallback_says_location_not_rules():
    """위치를 물었는데 '규정·공지에서 못 찾음'이라고 하지 않는다(교수님 #778)."""
    from backend.agent.nodes import Nodes
    from backend.domain.answer import FALLBACK_MESSAGE, LOCATION_FALLBACK_MESSAGE

    n = Nodes(deps(FakeLLM()))
    loc = n.fallback({"evidence_needs": ["location"], "evidence": [], "intent": "location"})
    assert loc["fallback"].message == LOCATION_FALLBACK_MESSAGE
    rule = n.fallback({"evidence_needs": ["eligibility_or_limit"], "evidence": []})
    assert rule["fallback"].message == FALLBACK_MESSAGE

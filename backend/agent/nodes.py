"""그래프 노드(상세설계 02 §4). 외부 의존(LLM·도구·부서 조회)은 AgentDeps로 주입한다."""

from __future__ import annotations

import asyncio
import json
import time
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from backend.agent import prompts
from backend.agent.llm import LLMTimeout, LLMUnavailable, StructuredLLM, StructuredOutputError
from backend.agent.needs import plan_calls
from backend.agent.resolve import NEED_PRIORITY, resolve
from backend.agent.slots import ask_text, choices_for, missing_slots, required_slots
from backend.agent.state import ActOut, ClassifyOut, ComposeOut, ToolCall, TurnState
from backend.agent.verify import SUPPRESSED_TEMPLATE, verify
from backend.app.clients import remaining
from backend.domain.answer import (
    Answer,
    AnswerNotice,
    Dept,
    Draft,
    Fallback,
    PublicSentence,
    Resolution,
)
from backend.domain.evidence import Evidence, EvidenceCard, ToolResult
from backend.domain.thread import LastTurn, PendingQuestion, Profile

ALLOWED_TOOLS = {
    "search_academic_knowledge",
    "get_notices",
    "get_academic_calendar",
    "get_menu",
    "find_campus_location",
}
TOOL_TEXT_LIMIT = 1500
MIN_SECONDS_FOR_TOOLS = 8.0  # 02 §3: 남은 시간 < 8초면 도구 호출 중단
MIN_SECONDS_FOR_COMPOSE = 4.0  # compose 진입 시 남은 시간 < 4초면 fallback(deadline)
SOFT_TOOLS_DONE = 5.0  # T+5초 체크포인트를 넘기면 보충 act 생략
COMPOSE_RESERVE = 2  # compose 1 + 재시도 1


DEFAULT_DEPT_BY_INTENT: dict[str, str] = {}


@dataclass
class AgentDeps:
    llm: StructuredLLM
    tools: dict[str, Any]  # 이름 → LangChain 도구(ainvoke) 또는 async 함수(**args) → ToolResult
    dept_lookup: Callable[[str], Dept | None] = lambda _id: None
    default_dept_id: Callable[[str | None], str | None] = lambda _intent: None
    glossary_confusables: list[set[str]] = field(default_factory=list)
    max_tool_calls: int = 4
    max_llm_calls: int = 6


def _emit(event: str, **data: Any) -> None:
    """LangGraph 커스텀 스트림(stream_mode="custom")으로 SSE 이벤트를 보낸다.

    스트리밍 실행이 아니면 무시한다.
    """
    try:
        from langgraph.config import get_stream_writer

        get_stream_writer()({"event": event, "data": data})
    except Exception:  # noqa: BLE001 — 스트림 밖 실행(단위 테스트 등)
        pass


def _nfc(text: str) -> str:
    """무해한 정규화(02 §4-2-1 1단계): NFKC(분해 자모 합성 포함) + 연속 공백 정리."""
    return " ".join(unicodedata.normalize("NFKC", text).split())


def _left(state: TurnState) -> float | None:
    return remaining(state.get("deadline"))


def _elapsed(state: TurnState) -> float:
    return time.monotonic() - state.get("started", time.monotonic())


class Nodes:
    def __init__(self, deps: AgentDeps):
        self.d = deps

    # ── classify ───────────────────────────────────────────────────────
    def _classify_prompt(self, state: TurnState) -> str:
        parts = [f"[학생 메시지]\n{_nfc(state['query'])}"]
        pq = state.get("pending_question")
        if pq:
            parts.append(f"[직전 되묻기]\n{pq.text}\n(원 질문: {pq.original_query_masked})")
        lt = state.get("last_turn")
        if lt:
            parts.append(
                f"[직전 턴]\n질문: {lt.query_masked}\n"
                f"답변 요약: {lt.answer_summary}\n주제: {lt.topic or ''}"
            )
        prof = state.get("profile") or Profile()
        parts.append(f"[알려진 조건]\n{prof.model_dump_json(exclude_none=True)}")
        return "\n\n".join(parts)

    def _block_confusables(self, out: ClassifyOut) -> ClassifyOut:
        """혼동쌍 사이 보정·숫자가 바뀌는 보정은 서버가 거부하고 확인 후보로 돌린다(02 §4-2-1)."""
        kept, cands = [], list(out.clarification_candidates)
        for c in out.corrections:
            confusable = any(c.from_ in grp and c.to in grp for grp in self.d.glossary_confusables)
            digits_changed = [ch for ch in c.from_ if ch.isdigit()] != [
                ch for ch in c.to if ch.isdigit()
            ]
            if confusable or digits_changed:
                cands += [x for x in (c.to, c.from_) if x not in cands]
            else:
                kept.append(c)
        if len(kept) == len(out.corrections):
            return out
        return out.model_copy(update={"corrections": kept, "clarification_candidates": cands})

    async def classify(self, state: TurnState) -> dict:
        _emit("status", step="classify", msg="질문 분석 중")
        calls = state.get("llm_calls_count", 0)
        out: ClassifyOut | None = None
        for _ in range(2):  # 구조화 출력 실패 시 1회 재시도
            calls += 1
            try:
                out = await self.d.llm.structured(
                    ClassifyOut,
                    prompts.CLASSIFY,
                    self._classify_prompt(state),
                    node="classify",
                    deadline=state.get("deadline"),
                )
                break
            except StructuredOutputError:
                continue
            except LLMUnavailable as e:
                return {
                    "llm_calls_count": calls,
                    "outcome": "error",
                    "error_code": "TIMEOUT" if isinstance(e, LLMTimeout) else "LLM_UNAVAILABLE",
                }
        if out is None:
            return {"llm_calls_count": calls, "outcome": "error", "error_code": "INTERNAL"}
        out = self._block_confusables(out)

        profile = (state.get("profile") or Profile()).merged(out.extracted_profile)
        pq = state.get("pending_question")
        clar = state.get("clarification_count", 0)
        normalized = out.normalized_query or _nfc(state["query"])
        if pq and out.answers_pending:
            effective = pq.original_query_masked
        else:
            if pq:  # 답하지 않고 새 질문 → pending 취소
                clar = 0
            effective = out.resolved_query if out.is_followup and out.resolved_query else normalized

        intent = out.intent if out.in_scope else "out_of_scope"
        # 필요 조건은 서버 표(slots.py)가 결정한다 — 모델의 needed_slots는 참고용 기록만(02 §4-2 표)
        needed = required_slots(out.topic, effective, intent)
        missing = missing_slots(needed, profile)
        shown = [{"from": c.from_, "to": c.to} for c in out.corrections if c.kind != "particle"]
        _emit("status", step="classify", msg="질문 분석 완료", corrections=shown)
        return {
            "llm_calls_count": calls,
            "intent": intent,
            "topic": out.topic,
            "profile": profile,
            "clarification_count": clar,
            "pending_question": pq if (pq and out.answers_pending) else None,
            "effective_query": effective,
            "search_query": out.search_query or effective,
            "normalized_query": normalized,
            "evidence_needs": list(dict.fromkeys(out.evidence_needs)),
            "corrections": out.corrections,
            "correction_confidence": out.confidence,
            "clarification_candidates": out.clarification_candidates,
            "missing_slots": missing,
        }

    # ── ask_user ───────────────────────────────────────────────────────
    def ask_user(self, state: TurnState) -> dict:
        missing = state.get("missing_slots", [])
        cands = state.get("clarification_candidates", [])
        text = ask_text(missing, confirm_term=cands[0] if cands else None)
        pq = PendingQuestion(
            text=text,
            original_query_masked=state.get("effective_query") or state["query"],
            missing_slots=missing,
            asked_at=datetime.now(UTC),
        )
        ask = {"question": text, "missing_slots": missing, "choices": choices_for(missing)}
        _emit("ask", **ask)
        return {
            "pending_question": pq,
            "clarification_count": state.get("clarification_count", 0) + 1,
            "ask": ask,
            "outcome": "ask",
        }

    # ── plan_tools · run_tools · act ───────────────────────────────────
    def plan_tools(self, state: TurnState) -> dict:
        calls = plan_calls(
            state.get("evidence_needs", []),
            search_query=state.get("search_query") or state.get("effective_query", ""),
            original_query=state.get("normalized_query"),
            topic=state.get("topic"),
            intent=state.get("intent"),
        )
        needs = state.get("evidence_needs", [])
        _emit(
            "status",
            step="plan",
            msg="필요한 근거: " + ", ".join(needs) if needs else "근거 계획 없음",
        )
        return {"required_calls": calls, "pending_tool_calls": calls}

    async def _invoke(self, call: ToolCall) -> ToolResult:
        tool = self.d.tools.get(call["name"])
        if tool is None:
            return ToolResult.fail("BAD_INPUT", f"unknown tool {call['name']}")
        r = await (tool.ainvoke(call["args"]) if hasattr(tool, "ainvoke") else tool(**call["args"]))
        return r if isinstance(r, ToolResult) else ToolResult.model_validate(r)

    async def run_tools(self, state: TurnState) -> dict:
        calls = list(state.get("pending_tool_calls", []))
        left = _left(state)
        if left is not None and left < MIN_SECONDS_FOR_TOOLS:
            calls = []  # 마감 임박 → 확보한 근거로 진행
        budget = self.d.max_tool_calls - state.get("tool_calls_count", 0)
        calls = calls[: max(budget, 0)]
        results = await asyncio.gather(*(self._invoke(c) for c in calls), return_exceptions=True)

        new_ev: list[Evidence] = []
        failures: list[str] = []
        msgs = []
        for c, r in zip(calls, results, strict=True):
            res = ToolResult.fail("EXCEPTION") if isinstance(r, BaseException) else r
            if res.ok:
                new_ev += res.items
            else:
                failures.append(c["name"])
            msgs.append(_tool_message(c, res))
            _emit(
                "status",
                step="act",
                tool=c["name"],
                count=len(res.items),
                ok=res.ok,
                stale=res.stale,
            )
        seen = {e.id for e in state.get("evidence", [])}
        fresh = [e for e in new_ev if e.id not in seen]
        if fresh:
            _emit("evidence", items=[EvidenceCard.from_evidence(e).model_dump() for e in fresh])
        merged = {e.id: e for e in state.get("evidence", []) + new_ev}
        evidence = list(merged.values())
        return {
            "messages": msgs,
            "evidence": evidence,
            "tool_calls_count": state.get("tool_calls_count", 0) + len(calls),
            "tool_failures": state.get("tool_failures", []) + failures,
            "missing_evidence_needs": _missing_needs(state.get("evidence_needs", []), evidence),
            "pending_tool_calls": [],
        }

    def llm_budget_allows_act(self, state: TurnState) -> bool:
        return state.get("llm_calls_count", 0) + 1 + COMPOSE_RESERVE <= self.d.max_llm_calls

    def act_allowed(self, state: TurnState) -> bool:
        left = _left(state)
        return (
            bool(state.get("missing_evidence_needs"))
            and not state.get("act_used", False)
            and self.llm_budget_allows_act(state)
            and state.get("tool_calls_count", 0) < self.d.max_tool_calls
            and (left is None or left >= MIN_SECONDS_FOR_TOOLS)
            and _elapsed(state) < SOFT_TOOLS_DONE
        )

    async def act(self, state: TurnState) -> dict:
        calls = state.get("llm_calls_count", 0) + 1
        user = json.dumps(
            {
                "질문": state.get("effective_query"),
                "부족한 근거": state.get("missing_evidence_needs", []),
                "실패한 도구": state.get("tool_failures", []),
                "이미 부른 호출": state.get("required_calls", []),
            },
            ensure_ascii=False,
        )
        try:
            out = await self.d.llm.structured(
                ActOut, prompts.ACT, user, node="act", deadline=state.get("deadline")
            )
        except (StructuredOutputError, LLMUnavailable):
            return {"llm_calls_count": calls, "act_used": True, "pending_tool_calls": []}
        return {
            "llm_calls_count": calls,
            "act_used": True,
            "pending_tool_calls": self._sanitize(out, state),
        }

    def _sanitize(self, out: ActOut, state: TurnState) -> list[ToolCall]:
        """허용 도구·입력 상한·실패 원천 재호출 금지(02 §4-5)."""
        failed = set(state.get("tool_failures", []))
        done = {
            (c["name"], json.dumps(c["args"], sort_keys=True, ensure_ascii=False))
            for c in state.get("required_calls", [])
        }
        room = self.d.max_tool_calls - state.get("tool_calls_count", 0)
        clean: list[ToolCall] = []
        for i, c in enumerate(out.calls):
            if c.name not in ALLOWED_TOOLS or c.name in failed:
                continue
            args = {k: (v[:100] if isinstance(v, str) else v) for k, v in c.args.items()}
            if c.name == "search_academic_knowledge":
                kinds = [k for k in args.get("kinds", []) if k in ("rule", "guide")]
                if not kinds or not args.get("query"):
                    continue
                args["kinds"] = kinds
            key = (c.name, json.dumps(args, sort_keys=True, ensure_ascii=False))
            if key in done:
                continue
            done.add(key)
            clean.append({"id": f"act_{i}", "name": c.name, "args": args})
        return clean[: max(min(room, 2), 0)]

    # ── resolve_evidence ───────────────────────────────────────────────
    def resolve_evidence(self, state: TurnState) -> dict:
        r = resolve(state.get("evidence", []), state.get("evidence_needs", []))
        return {"resolution": r, "review_flags": state.get("review_flags", []) + r.review_flags}

    # ── compose · verify ───────────────────────────────────────────────
    def _compose_prompt(self, state: TurnState) -> str:
        r = state.get("resolution") or Resolution()
        ev = [
            {
                "id": e.id,
                "kind": e.kind,
                "title": e.title,
                "text": e.text[:TOOL_TEXT_LIMIT],
                "meta": _safe_meta(e.meta),
            }
            for e in state.get("evidence", [])
        ]
        prof = state.get("profile") or Profile()
        return json.dumps(
            {
                "질문": state.get("effective_query"),
                "학생 조건": prof.model_dump(exclude_none=True),
                "조건 부족(가정 금지)": state.get("missing_slots", []),
                "채택 근거": r.adopted,
                "근거": ev,
            },
            ensure_ascii=False,
        )

    async def compose(self, state: TurnState) -> dict:
        left = _left(state)
        if left is not None and left < MIN_SECONDS_FOR_COMPOSE:
            return {"fallback_reason": "deadline"}
        if not state.get("evidence"):
            reason = "tool_failure" if state.get("tool_failures") else "no_evidence"
            return {"fallback_reason": reason}
        _emit("status", step="compose", msg="답변 작성 중")
        calls = state.get("llm_calls_count", 0)
        draft: Draft | None = None
        for _ in range(2):
            if calls >= self.d.max_llm_calls:
                break
            calls += 1
            try:
                out = await self.d.llm.structured(
                    ComposeOut,
                    prompts.COMPOSE,
                    self._compose_prompt(state),
                    node="compose",
                    deadline=state.get("deadline"),
                )
                draft = out.to_draft() if isinstance(out, ComposeOut) else out
                break
            except StructuredOutputError:
                continue
            except LLMTimeout:
                # 근거는 확보했지만 시간 안에 답을 못 만듦 → 설계상 fallback(deadline)(02 §7)
                return {"llm_calls_count": calls, "fallback_reason": "deadline"}
            except LLMUnavailable:
                return {
                    "llm_calls_count": calls,
                    "outcome": "error",
                    "error_code": "LLM_UNAVAILABLE",
                }
        if draft is None:
            return {"llm_calls_count": calls, "fallback_reason": "verification_failed"}
        return {"llm_calls_count": calls, "draft": draft}

    def verify(self, state: TurnState) -> dict:
        _emit("status", step="verify", msg="근거 확인 중")
        draft = state.get("draft") or Draft()
        verified, report, flags = verify(
            draft,
            state.get("evidence", []),
            state.get("profile") or Profile(),
            state.get("resolution"),
        )
        out: dict[str, Any] = {
            "draft": verified,
            "verify_report": report,
            "review_flags": state.get("review_flags", []) + flags,
        }
        if not verified.sentences:
            out["fallback_reason"] = "verification_failed"
        return out

    # ── answer · fallback ──────────────────────────────────────────────
    def _dept(self, state: TurnState, cited: list[str]) -> Dept | None:
        ev = {e.id: e for e in state.get("evidence", [])}
        pool = [ev[c] for c in cited if c in ev] or state.get("evidence", [])
        if "procedure_and_contact" in state.get("evidence_needs", []):
            guides = [e for e in pool if e.kind == "guide" and e.meta.get("dept_id")]
            pool = guides or pool
        ids = [e.meta["dept_id"] for e in pool if e.meta.get("dept_id")]
        dept_id = (
            Counter(ids).most_common(1)[0][0]
            if ids
            else self.d.default_dept_id(state.get("intent"))
        )
        return self.d.dept_lookup(dept_id) if dept_id else None

    def answer(self, state: TurnState) -> dict:
        draft = state.get("draft") or Draft()
        r = state.get("resolution") or Resolution()
        cited = list(
            dict.fromkeys(
                c
                for s in draft.sentences + draft.checklist + draft.next_actions
                for c in s.cite_ids
            )
        )
        ev = {e.id: e for e in state.get("evidence", [])}
        stale = [ev[c] for c in cited if c in ev and ev[c].meta.get("stale")]
        notices = [AnswerNotice(code="source_conflict", text=t) for t in r.template_sentences]
        if any(f.public_action == "suppress_sentence" for f in state.get("review_flags", [])):
            notices.append(AnswerNotice(code="suppressed", text=SUPPRESSED_TEMPLATE))
        as_of = min((str(e.meta.get("as_of")) for e in stale if e.meta.get("as_of")), default=None)
        ans = Answer(
            notices=notices,
            sentences=[PublicSentence.from_draft(s) for s in draft.sentences],
            checklist=[PublicSentence.from_draft(s) for s in draft.checklist],
            next_actions=[PublicSentence.from_draft(s) for s in draft.next_actions],
            dept=self._dept(state, cited),
            cited=cited,
            as_of=as_of,
            stale_used=bool(stale),
        )
        _emit("answer", **ans.model_dump(mode="json"))
        return {"answer": ans, "outcome": "answer"}

    def fallback(self, state: TurnState) -> dict:
        reason = state.get("fallback_reason") or (
            "out_of_scope" if state.get("intent") == "out_of_scope" else "no_evidence"
        )
        top = [e.id for e in state.get("evidence", [])[:5]]
        fb = Fallback(
            reason=reason, dept=self._dept(state, top) if reason != "out_of_scope" else None
        )
        _emit("fallback", **fb.model_dump(mode="json"))
        return {"fallback": fb, "fallback_reason": reason, "outcome": "fallback"}

    # ── save_state(완료 트랜잭션 입력 계산만 — 쓰기는 API 계층) ─────────
    def save_state(self, state: TurnState) -> dict:
        outcome = state.get("outcome")
        if outcome == "error":
            return {}  # threads를 건드리지 않아 기존 pending 보존
        update: dict[str, Any] = {}
        if outcome != "ask":
            update["pending_question"] = None
            update["clarification_count"] = 0
        if outcome == "answer" and state.get("answer"):
            ans = state["answer"]
            summary = " ".join(s.text for s in ans.sentences)[:300]
            update["last_turn"] = LastTurn(
                query_masked=state.get("effective_query") or state["query"],
                answer_summary=summary,
                cited_ids=ans.cited,
                topic=state.get("topic"),
            )
        return update


def _safe_meta(meta: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "department",
        "revision_date",
        "has_table",
        "stale",
        "as_of",
        "page_modified",
        "published_at",
        "semester",
    )
    return {k: (str(v) if isinstance(v, datetime) else v) for k, v in meta.items() if k in keep}


def _tool_message(call: ToolCall, res: ToolResult):
    from langchain_core.messages import ToolMessage

    body = {
        "ok": res.ok,
        "error_code": res.error_code,
        "message": res.message,
        "items": [
            {"id": e.id, "title": e.title, "text": e.text[:TOOL_TEXT_LIMIT]} for e in res.items
        ],
    }
    return ToolMessage(
        content=json.dumps(body, ensure_ascii=False), tool_call_id=call["id"], name=call["name"]
    )


def _missing_needs(needs: list[str], evidence: list[Evidence]) -> list[str]:
    """우선 출처 종류의 근거가 0건인 사실 종류. 보조 출처만 있으면 부족으로 본다
    (예: 이번 학기 마감에 규정의 일반 기간만 있음 → 일정·공지 보충 대상)."""
    kinds = {e.kind for e in evidence}
    return [n for n in needs if not (kinds & set(NEED_PRIORITY.get(n, ((), ()))[0]))]

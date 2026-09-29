"""노드·엣지 조립(상세설계 02 §3). load_state·완료 트랜잭션은 API 계층이 맡는다(04 §2-1)."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from backend.agent.nodes import AgentDeps, Nodes
from backend.agent.state import TurnState


def build_graph(deps: AgentDeps):
    n = Nodes(deps)
    g = StateGraph(TurnState)
    g.add_node("classify", n.classify)
    g.add_node("ask_user", n.ask_user)
    g.add_node("plan_tools", n.plan_tools)
    g.add_node("run_tools", n.run_tools)
    g.add_node("act", n.act)
    g.add_node("resolve_evidence", n.resolve_evidence)
    g.add_node("compose", n.compose)
    g.add_node("verify", n.verify)
    g.add_node("answer", n.answer)
    g.add_node("fallback", n.fallback)
    g.add_node("save_state", n.save_state)

    def after_classify(s: TurnState) -> str:
        if s.get("outcome") == "error":
            return "save_state"
        if s.get("intent") == "out_of_scope":
            return "fallback"
        if (s.get("missing_slots") or s.get("clarification_candidates")) and s.get(
            "clarification_count", 0
        ) == 0:
            return "ask_user"
        return "plan_tools"

    def after_run_tools(s: TurnState) -> str:
        return "act" if n.act_allowed(s) else "resolve_evidence"

    def after_act(s: TurnState) -> str:
        if s.get("pending_tool_calls") and s.get("tool_calls_count", 0) < deps.max_tool_calls:
            return "run_tools"
        return "resolve_evidence"

    def after_compose(s: TurnState) -> str:
        if s.get("outcome") == "error":
            return "save_state"
        return "verify" if s.get("draft") is not None else "fallback"

    def after_verify(s: TurnState) -> str:
        d = s.get("draft")
        return "answer" if d is not None and d.sentences else "fallback"

    g.add_edge(START, "classify")
    g.add_conditional_edges(
        "classify", after_classify, ["save_state", "fallback", "ask_user", "plan_tools"]
    )
    g.add_edge("ask_user", "save_state")
    g.add_edge("plan_tools", "run_tools")
    g.add_conditional_edges("run_tools", after_run_tools, ["act", "resolve_evidence"])
    g.add_conditional_edges("act", after_act, ["run_tools", "resolve_evidence"])
    g.add_edge("resolve_evidence", "compose")
    g.add_conditional_edges("compose", after_compose, ["save_state", "verify", "fallback"])
    g.add_conditional_edges("verify", after_verify, ["answer", "fallback"])
    g.add_edge("answer", "save_state")
    g.add_edge("fallback", "save_state")
    g.add_edge("save_state", END)
    return g.compile()

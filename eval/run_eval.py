"""평가 실행기(상세설계 06 §2~§5).

    uv run python -m eval.run_eval --set dev [--limit N] [--no-cache]

- 자동 판정: ①기대 근거 검색 ④유효 인용 ⑤행동·부서 / 범위 밖은 fallback(out_of_scope)
- 자동 판정: ①기대 근거 검색 ④유효 인용 ⑤행동·부서 / 범위 밖은 fallback(out_of_scope)
- LLM 판정: ②핵심 사실 포함 ③잘못된 사실 없음 — **채점 모델은 서비스 모델과 분리**(GRADER_MODEL)
- 결과: eval/reports/{날짜}_{set}_{커밋}.json + .md
- 캐시: eval/cache/{문항해시}_{커밋}_{색인}_{모델}_{프롬프트}.json
- final 세트(eval/final.jsonl)는 저장소 밖에서만 둔다(.gitignore)
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import os
import statistics
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

EVAL_DIR = Path(__file__).resolve().parent
REPORTS = EVAL_DIR / "reports"
CACHE = EVAL_DIR / "cache"
SETS = {
    "dev": "dev.jsonl",
    "oos": "oos.jsonl",
    "final": "final.jsonl",
    "sample": "sample.jsonl",
    "questions20": "questions20.jsonl",
}
TYPE_CRITERIA = {  # 06 §3-1 적용표(●=필수)
    "rule": {"retrieval", "key_facts", "no_errors", "citation"},
    "procedure": {"retrieval", "key_facts", "no_errors", "citation", "action"},
    "notice": {"key_facts", "no_errors", "citation"},
    "calendar": {"key_facts", "no_errors", "citation"},
    "menu": {"key_facts", "no_errors", "citation"},
    "location": {"key_facts", "no_errors", "citation"},
    "oos": {"oos_fallback"},
}
INDEX_VERSION = "articles-v1"


# ── 실행 ────────────────────────────────────────────────────────────────
def _initial(query: str, thread: dict[str, Any]) -> dict[str, Any]:
    from backend.app.clients import deadline_after
    from backend.app.config import get_settings
    from backend.domain import Profile
    from backend.store.mask import mask

    return {
        "thread_id": "eval",
        "request_id": "eval",
        "query": mask(query),
        "started": time.monotonic(),
        "deadline": deadline_after(get_settings().hard_deadline_ms),
        "profile": thread.get("profile") or Profile(),
        "pending_question": thread.get("pending_question"),
        "clarification_count": thread.get("clarification_count", 0),
        "last_turn": None,
        "evidence": [],
        "tool_calls_count": 0,
        "llm_calls_count": 0,
        "tool_failures": [],
        "review_flags": [],
        "act_used": False,
        "pending_tool_calls": [],
    }


async def run_item(graph: Any, item: dict[str, Any]) -> dict[str, Any]:
    turns: list[dict[str, Any]] = []
    thread: dict[str, Any] = {}
    query = item["question"]
    for _ in range(2):
        t0 = time.monotonic()
        final = await graph.ainvoke(_initial(query, thread))
        turns.append(_summarize(final, int((time.monotonic() - t0) * 1000)))
        if final.get("outcome") != "ask" or not item.get("profile_answer"):
            break
        thread = {
            "profile": final.get("profile"),
            "pending_question": final.get("pending_question"),
            "clarification_count": final.get("clarification_count", 1),
        }
        query = item["profile_answer"]
    return {"id": item["id"], "type": item.get("type", "oos"), "turns": turns}


def _summarize(final: dict[str, Any], elapsed_ms: int) -> dict[str, Any]:
    ans, fb, vr = final.get("answer"), final.get("fallback"), final.get("verify_report")
    sentences = (ans.sentences + ans.checklist + ans.next_actions) if ans else []
    evidence = final.get("evidence", [])
    return {
        "outcome": final.get("outcome"),
        "fallback_reason": final.get("fallback_reason"),
        "elapsed_ms": elapsed_ms,
        "retrieved": [{"id": e.id, "kind": e.kind} for e in evidence],
        "cited": ans.cited if ans else [],
        "cited_text": "\n\n".join(e.text[:1500] for e in evidence if ans and e.id in ans.cited),
        "text": "\n".join(s.text for s in sentences),
        "has_actions": bool(ans and (ans.checklist or ans.next_actions)),
        "dept": ans.dept.name if ans and ans.dept else (fb.dept.name if fb and fb.dept else None),
        "verify_total": vr.total if vr else 0,
        "verify_kept": vr.kept if vr else 0,
        "llm_calls": final.get("llm_calls_count", 0),
    }


# ── 판정 ────────────────────────────────────────────────────────────────
class Grade(BaseModel):
    missing_key_facts: list[str] = Field(default_factory=list)
    has_contradiction_or_forbidden: bool = False
    reason: str = ""


GRADER_PROMPT = """당신은 대학 학사 안내 답변을 채점합니다. JSON으로만 답합니다.
- missing_key_facts: [핵심 사실] 중 답변에 담기지 않은 항목(표현이 달라도 의미가 같으면 담긴 것)
- has_contradiction_or_forbidden: 답변이 [금지 사실]을 포함하거나 [근거]와 모순되면 true
- reason: 한 문장"""


async def llm_grade(item: dict[str, Any], text: str, evidence_text: str) -> Grade:
    if not item.get("key_facts") and not item.get("forbidden_facts"):
        return Grade()
    from langchain_google_genai import ChatGoogleGenerativeAI

    from backend.app.clients import call_with_retry
    from backend.app.config import get_settings

    s = get_settings()
    chat = ChatGoogleGenerativeAI(
        model=os.environ.get("GRADER_MODEL", "gemini-3.5-flash-lite"),
        vertexai=True,
        project=s.gcp_project_id or None,
        location=s.gemini_location,
        temperature=0,
        max_retries=0,
    ).with_structured_output(Grade)
    user = json.dumps(
        {
            "질문": item["question"],
            "핵심 사실": item.get("key_facts", []),
            "금지 사실": item.get("forbidden_facts", []),
            "답변": text,
            "근거": evidence_text[:6000],
        },
        ensure_ascii=False,
    )

    async def call():
        return await chat.ainvoke([("system", GRADER_PROMPT), ("human", user)])

    return await call_with_retry(call, timeout=30, attempts=5, target="grader")


def judge(item: dict[str, Any], run: dict[str, Any], grade: Grade | None) -> dict[str, Any]:
    last = run["turns"][-1]
    need = TYPE_CRITERIA.get(item.get("type", "oos"), TYPE_CRITERIA["rule"])
    ids = [r["id"] for r in last["retrieved"]]
    top5 = set(ids[:5])
    res: dict[str, bool] = {}
    if "retrieval" in need:
        exp = item.get("expected_evidence_ids") or {}
        any_of, all_of = exp.get("any_of", []), exp.get("all_of", [])
        ok_ids = (not any_of or any(i in top5 for i in any_of)) and all(i in top5 for i in all_of)
        kinds = {r["kind"] for r in last["retrieved"]}
        want = {"article" if k == "rule" else k for k in item.get("expected_source_kinds", [])}
        res["retrieval"] = ok_ids and want <= kinds
    if "citation" in need:
        res["citation"] = last["outcome"] == "answer" and bool(last["cited"])
    if "action" in need:
        dept_ok = not item.get("expected_dept") or last["dept"] == item["expected_dept"]
        res["action"] = last["has_actions"] and dept_ok
    if "key_facts" in need:
        res["key_facts"] = grade is not None and not grade.missing_key_facts
    if "no_errors" in need:
        res["no_errors"] = grade is not None and not grade.has_contradiction_or_forbidden
    if "oos_fallback" in need:
        res["oos_fallback"] = (
            last["outcome"] == "fallback" and last["fallback_reason"] == "out_of_scope"
        )
    return {"criteria": res, "correct": all(res.values())}


# ── 지표·리포트 ─────────────────────────────────────────────────────────
def _commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        )
        return out.stdout.strip()
    except Exception:  # noqa: BLE001
        return "nogit"


def _cache_path(item: dict[str, Any], commit: str) -> Path:
    from backend.agent.prompts import PROMPT_VERSION
    from backend.app.config import get_settings

    raw = json.dumps(item, sort_keys=True, ensure_ascii=False).encode()
    h = hashlib.sha256(raw).hexdigest()[:16]
    model = get_settings().gemini_model
    return CACHE / f"{h}_{commit}_{INDEX_VERSION}_{model}_{PROMPT_VERSION}.json"


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 3) if den else None


def has_reference_labels(item: dict[str, Any]) -> bool:
    """Whether correctness can be compared with a human-authored reference."""
    if item.get("type") == "oos":
        return True
    evidence = item.get("expected_evidence_ids") or {}
    return bool(
        evidence.get("any_of")
        or evidence.get("all_of")
        or item.get("expected_source_kinds")
        or item.get("key_facts")
        or item.get("forbidden_facts")
        or item.get("expected_dept")
    )


def metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    lat = sorted(t["elapsed_ms"] for r in results for t in r["run"]["turns"])
    with_ret = [r for r in results if "retrieval" in r["judge"]["criteria"]]
    answers = [r for r in results if r["run"]["turns"][-1]["outcome"] == "answer"]
    labeled = [r for r in results if r.get("labeled", True)]
    workflow_values = [
        passed
        for r in results
        for criterion, passed in r["judge"]["criteria"].items()
        if criterion in {"citation", "action", "oos_fallback"}
    ]
    total = sum(t["verify_total"] for r in results for t in r["run"]["turns"])
    kept = sum(t["verify_kept"] for r in results for t in r["run"]["turns"])
    return {
        "items": len(results),
        "labeled_items": len(labeled),
        "correct": sum(r["judge"]["correct"] for r in labeled),
        "accuracy": _ratio(sum(r["judge"]["correct"] for r in labeled), len(labeled)),
        "recall_at_5": _ratio(
            sum(r["judge"]["criteria"]["retrieval"] for r in with_ret), len(with_ret)
        ),
        "verified_retention": _ratio(kept, total),
        "answer_with_citation_rate": _ratio(
            sum(bool(r["run"]["turns"][-1]["cited"]) for r in answers), len(answers)
        ),
        "workflow_criteria_rate": _ratio(sum(workflow_values), len(workflow_values)),
        "latency_ms": {
            "mean": round(statistics.mean(lat)) if lat else None,
            "p50": round(statistics.median(lat)) if lat else None,
            "p95": lat[max(0, int(len(lat) * 0.95) - 1)] if lat else None,
        },
    }


def write_report(set_name: str, commit: str, results: list[dict[str, Any]]) -> Path:
    from backend.agent.prompts import PROMPT_VERSION
    from backend.app.config import get_settings

    REPORTS.mkdir(parents=True, exist_ok=True)
    day = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")
    m = metrics(results)
    base = REPORTS / f"{day}_{set_name}_{commit}"
    base.with_suffix(".json").write_text(
        json.dumps({"metrics": m, "results": results}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    lat = m["latency_ms"]
    lines = [
        f"# 평가 리포트 — {set_name} — {day} — 커밋 {commit} — 색인 {INDEX_VERSION} — "
        f"모델 {get_settings().gemini_model} — 프롬프트 {PROMPT_VERSION}",
        "",
        "| 지표 | 값 |",
        "|---|---|",
        f"| 정답률 | {m['accuracy']} ({m['correct']}/{m['labeled_items']}) |"
        if m["accuracy"] is not None
        else "| 정답률 | — (참조 라벨 없음) |",
        f"| Recall@5 | {m['recall_at_5']} |",
        f"| Verified Retention | {m['verified_retention']} |",
        f"| 답변 중 근거 인용 포함률 | {m['answer_with_citation_rate']} |",
        f"| 워크플로 기준 충족률 | {m['workflow_criteria_rate']} |",
        f"| 응답 시간 평균·p50·p95(ms) | {lat['mean']} · {lat['p50']} · {lat['p95']} |",
        "",
        "## 오답 목록",
        "| id | 실패 기준 | 결과 | 채점 메모 |",
        "|---|---|---|---|",
    ]
    for r in results:
        if r.get("labeled", True) and not r["judge"]["correct"]:
            failed = ", ".join(k for k, v in r["judge"]["criteria"].items() if not v)
            last = r["run"]["turns"][-1]
            outcome = f"{last['outcome']} {last['fallback_reason'] or ''}".strip()
            lines.append(f"| {r['id']} | {failed} | {outcome} | {r.get('grade_reason', '')} |")
    lines += ["", "## 문항별 답변(사람 검토용)"]
    for r in results:
        last = r["run"]["turns"][-1]
        turns = " → ".join(t["outcome"] or "?" for t in r["run"]["turns"])
        ms = " + ".join(str(t["elapsed_ms"]) for t in r["run"]["turns"])
        body = last["text"] or f"(답변 없음: {last['fallback_reason'] or last['outcome']})"
        lines += [
            "",
            f"### {r['id']} — {turns} · {ms}ms · 인용 {len(last['cited'])}"
            f" · 담당 {last['dept'] or '—'}",
            f"> {r.get('question', '')}",
            "",
            body.replace("\n", "  \n"),
        ]
    base.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return base


async def evaluate(set_name: str, limit: int | None, use_cache: bool) -> Path:
    from backend.agent.graph import build_graph
    from backend.app.config import get_settings
    from backend.app.main import default_deps

    path = EVAL_DIR / SETS[set_name]
    items = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    items = items[:limit] if limit else items
    graph = build_graph(default_deps(get_settings()))
    commit = _commit()
    CACHE.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(3)

    async def one(i: int, item: dict[str, Any]) -> dict[str, Any]:
        cp = _cache_path(item, commit)
        if use_cache and cp.exists():
            return json.loads(cp.read_text(encoding="utf-8"))
        async with sem:
            await asyncio.sleep(0.3 * (i % 3))
            run = await run_item(graph, item)
        last = run["turns"][-1]
        grade = None
        if TYPE_CRITERIA.get(item.get("type", "oos"), set()) & {"key_facts", "no_errors"}:
            grade = await llm_grade(item, last["text"], last["cited_text"])
        result = {
            "id": item["id"],
            "question": item["question"],
            "labeled": has_reference_labels(item),
            "run": run,
            "judge": judge(item, run, grade),
            "grade_reason": grade.reason if grade else "",
        }
        cp.write_text(json.dumps(result, ensure_ascii=False, default=str), encoding="utf-8")
        return result

    results = await asyncio.gather(*(one(i, it) for i, it in enumerate(items)))
    return write_report(set_name, commit, list(results))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a campusBridge evaluation set")
    parser.add_argument("--set", dest="dataset", choices=tuple(SETS), required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--no-cache", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base = asyncio.run(evaluate(args.dataset, args.limit, not args.no_cache))
    print(base.with_suffix(".md").read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

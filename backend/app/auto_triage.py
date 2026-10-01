# ruff: noqa: E501 — 설명 문장은 줄바꿈하지 않는다
"""밤사이 자동 처리 — 미응답·피드백·잘못된 정보 제보(교수님 2026-10-01 CLI).

매일 새벽 수집(05:00 KST)이 끝난 뒤 돌아, 관리자가 아침에 결과만 보게 한다.
- 미응답 질문: 지금 색인으로 다시 물어 답하면 '검증완료'(관리자 [재확인]과 같은 함수)
- 피드백(별로예요): 그 질문을 다시 물어 지금 답이 되는지 표시만 한다(종결은 사람이)
- 잘못된 정보 제보: 사람 승인이 필요 없는 다음 단계 하나(분석·재확인 2/2 자동 종결, 되돌리기 가능)
  **공식 자료를 바꾸는 [승인·반영]은 하지 않는다** — 승인 대기 건수만 센다
결과는 auto_runs/{날짜}(KST)에 남고 관리자 화면 위에 "밤사이 자동 처리" 줄로 보인다.

실행: python -m backend.app.auto_triage   (Cloud Run Job campusbridge-auto, 매일 06:00 KST)
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

log = logging.getLogger("campusbridge.auto")
KST = timezone(timedelta(hours=9), "KST")
MAX_UNANSWERED = 60
MAX_FEEDBACK = 40
MAX_REPORTS = 40
SYSTEM_EMAIL = "system:auto-triage"


@dataclass(frozen=True)
class SystemActor:
    """감사 로그에 남는 자동 처리 주체(사람 관리자와 구분)."""

    email: str = SYSTEM_EMAIL
    subject: str = "auto-triage"
    role: str = "admin"


async def run_unanswered(
    uids: list[str], recheck: Callable[[str], Awaitable[dict[str, Any]]]
) -> Counter:
    c: Counter = Counter()
    for uid in uids[:MAX_UNANSWERED]:
        try:
            got = await recheck(uid)
            c["verified" if got.get("status") == "verified" else "still_unanswered"] += 1
        except Exception as exc:  # noqa: BLE001 — 한 건 실패가 전체를 멈추지 않게
            log.warning("auto unanswered %s failed: %s", uid[:12], type(exc).__name__)
            c["error"] += 1
    return c


async def run_reports(rids: list[str], step: Callable[[str], Awaitable[dict[str, Any]]]) -> Counter:
    c: Counter = Counter()
    for rid in rids[:MAX_REPORTS]:
        try:
            got = await step(rid)
        except Exception as exc:  # noqa: BLE001
            log.warning("auto report %s failed: %s", rid, type(exc).__name__)
            c["error"] += 1
            continue
        action = got.get("action")
        if action == "verify":
            c["resolved" if got.get("status") == "verified" else "recheck_failed"] += 1
        elif action == "analyze":
            c["analyzed"] += 1
        elif got.get("needs_human"):
            c["needs_approval"] += 1
        elif action == "wait":
            c["waiting"] += 1
        else:
            c["skipped"] += 1
    return c


async def run_feedback(
    rows: list[tuple[str, dict[str, Any]]],
    query_of: Callable[[str], Awaitable[str | None]],
    run_turn: Callable[[str], Awaitable[dict[str, Any]]],
    mark: Callable[[str, dict[str, Any]], Awaitable[None]],
) -> Counter:
    """별로예요 피드백: 질문을 다시 물어 지금 답이 되는지 표시(recheck 필드). 종결은 사람이."""
    from backend.reports.resolve import run_ok

    c: Counter = Counter()
    for fid, row in rows[:MAX_FEEDBACK]:
        try:
            q = await query_of(str(row.get("turn_id") or fid))
            if not q:
                c["no_question"] += 1
                await mark(fid, {"recheck": {"at": datetime.now(UTC), "result": "no_question"}})
                continue
            ok, why = run_ok(await run_turn(q), None)
            c["now_answers" if ok else "still_fails"] += 1
            await mark(
                fid,
                {
                    "recheck": {
                        "at": datetime.now(UTC),
                        "result": "now_answers" if ok else "still_fails",
                        "reason": None if ok else why,
                    }
                },
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("auto feedback %s failed: %s", fid[:12], type(exc).__name__)
            c["error"] += 1
    return c


def summary_line(s: dict[str, Any]) -> str:
    """관리자 화면 한 줄: '밤사이 자동 처리 — 미응답 검증완료 3 · 제보 종결 1 · 승인 대기 2'."""
    u, r, f = s.get("unanswered", {}), s.get("reports", {}), s.get("feedback", {})
    parts = [
        f"미응답 검증완료 {u.get('verified', 0)}/{sum(u.values())}",
        f"제보 자동 종결 {r.get('resolved', 0)}",
        f"제보 분석 {r.get('analyzed', 0)}",
        f"승인 대기 {r.get('needs_approval', 0)}",
        f"피드백 지금 답함 {f.get('now_answers', 0)}/{sum(f.values())}",
    ]
    return " · ".join(parts)


async def run_all(db: Any, r: Any, s: Any) -> dict[str, Any]:
    from backend.app.main import fix_deps, recheck_state, recheck_unanswered_row
    from backend.reports import agent_fix

    actor = SystemActor()
    uids = [
        d.id
        async for d in db.collection("unanswered").stream()
        if (d.to_dict() or {}).get("status") != "verified"
    ]
    unanswered = await run_unanswered(uids, lambda uid: recheck_unanswered_row(r, s, uid))

    d = fix_deps(r, s, actor)
    rows = await d.reports.list(type_="wrong_info")
    rids = [
        str(x.get("id")) for x in rows if x.get("status") not in agent_fix.CLOSED and x.get("id")
    ]
    reports = await run_reports(rids, lambda rid: agent_fix.auto_step(d, rid, actor))

    fb_rows = [(x.id, x.to_dict() or {}) async for x in db.collection("feedback").stream()]
    fb_rows = [(i, x) for i, x in fb_rows if x.get("rating") == -1 and not x.get("recheck")]

    async def query_of(turn_id: str) -> str | None:
        snap = await db.collection("turns").document(turn_id).get()
        return (snap.to_dict() or {}).get("query_masked") if snap.exists else None

    async def run_turn(q: str) -> dict[str, Any]:
        return await r.graph.ainvoke(recheck_state(q, s))

    async def mark(fid: str, fields: dict[str, Any]) -> None:
        await db.collection("feedback").document(fid).set(fields, merge=True)

    feedback = await run_feedback(fb_rows, query_of, run_turn, mark)
    out: dict[str, Any] = {
        "unanswered": dict(unanswered),
        "reports": dict(reports),
        "feedback": dict(feedback),
        "app_version": s.app_version,
        "created_at": datetime.now(UTC),
    }
    out["summary"] = summary_line(out)
    day = datetime.now(KST).date().isoformat()
    await db.collection("auto_runs").document(day).set(out)
    return out


def main() -> int:
    import time

    from backend.agent.graph import build_graph
    from backend.app.config import get_settings
    from backend.app.main import TurnRunner, default_deps, default_store

    logging.basicConfig(level=logging.INFO)
    s = get_settings()
    deps = default_deps(s)
    store = default_store(s)
    r = TurnRunner(graph=build_graph(deps), store=store, settings=s, deps=deps)
    t0 = time.monotonic()
    out = asyncio.run(run_all(store.db, r, s))
    log.info(
        json.dumps(
            {
                "event": "auto_triage",
                "summary": out["summary"],
                "elapsed_s": round(time.monotonic() - t0),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

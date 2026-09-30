# ruff: noqa: E501 — 안내 문구·지시문은 줄바꿈하지 않는다
"""제보함 에이전트 조치 흐름: 분석(analyze) → 승인·반영(apply) → 해결 판정(verify).

각 단계는 제보 문서의 `agent_fix`에 남고 감사 로그에 기록된다. 종결은 되돌리기가 가능하다(undo).
의존성(검색·모델·그래프·저장소)은 호출하는 쪽이 주입한다 — 테스트에서 가짜로 바꿀 수 있게.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from backend.domain import AppError
from backend.reports import resolve


@dataclass
class FixDeps:
    reports: Any  # ReportStore
    fetch_text: Callable[[str], Awaitable[str]]
    search: Callable[[str], Awaitable[list[dict[str, Any]]]]
    extract: Callable[[str], Awaitable[resolve.PlaceDraftOut]]
    run_turn: Callable[[str], Awaitable[dict[str, Any]]]
    paraphrase: Callable[[str], Awaitable[str | None]]
    save_place: Callable[[dict[str, Any]], Awaitable[str]]  # 초안 → 검수 완료 장소(place_id)
    run_status: Callable[[str], Awaitable[dict[str, Any] | None]]


async def _audit(d: FixDeps, actor: Any, rid: str, action: str, after: dict[str, Any]) -> None:
    now = datetime.now(UTC)
    await d.reports.audit(
        {
            "actor": actor.email,
            "actor_sub": actor.subject,
            "action": f"report.agent.{action}",
            "target": rid,
            "before": None,
            "after": {k: v for k, v in after.items() if k in ("type", "status", "place_id")},
            "reason": "제보함 에이전트 조치",
            "request_id": f"report-{rid}-agent-{action}-{now.timestamp():.0f}",
            "result": "success",
            "created_at": now,
        }
    )


async def _row(d: FixDeps, rid: str) -> dict[str, Any]:
    row = await d.reports.get(rid)
    if not row or row.get("type") != "wrong_info":
        raise AppError("BAD_REQUEST", "잘못된 정보 제보만 에이전트 조치를 할 수 있습니다.")
    return row


async def analyze(d: FixDeps, rid: str, actor: Any) -> dict[str, Any]:
    row = await _row(d, rid)
    snap = row.get("snapshot") or {}
    kind = resolve.fix_type(row)
    fix: dict[str, Any] = {"type": kind, "analyzed_at": datetime.now(UTC)}
    if kind == "place":
        draft = await resolve.draft_place(
            snap.get("question_masked") or "", row.get("text_masked") or "", d.search, d.extract
        )
        fix.update(status="proposed" if draft["ok"] else "no_draft", draft=draft)
    elif kind == "recollect":
        pages = await resolve.compare_sources(snap, d.fetch_text)
        sources = sorted({p["source"] for p in pages if p["status"] == "changed" and p["source"]})
        fix.update(
            status="proposed" if sources else "no_change",
            pages=pages,
            source_ids=sources,
        )
    else:  # ③ 미응답 목록에 연결 — 자료 보강 뒤 재확인
        await d.reports.add_unanswered(snap.get("question_masked") or "")
        fix.update(status="linked")
    await d.reports.update(rid, {"agent_fix": fix})
    await _audit(d, actor, rid, "analyze", fix)
    return fix


async def apply(d: FixDeps, rid: str, actor: Any, run_id: str | None = None) -> dict[str, Any]:
    """사람의 [승인·반영] 1회. 장소는 장소표에 반영 후 바로 해결 판정, 재수집은 실행 번호를 기록."""
    row = await _row(d, rid)
    fix = dict(row.get("agent_fix") or {})
    now = datetime.now(UTC)
    if fix.get("type") == "place" and fix.get("status") in ("proposed", "recheck_failed"):
        draft = fix.get("draft") or {}
        drafted = draft.get("drafted_at")
        if not isinstance(drafted, datetime) or now - drafted > resolve.DRAFT_TTL:
            raise AppError(
                "BAD_REQUEST", "초안이 오래되었습니다. [조치안 다시 만들기]를 눌러 주세요."
            )
        if fix.get("status") == "proposed":
            fix["place_id"] = await d.save_place(draft)
        fix.update(status="applied", applied_at=now, applied_by=actor.email)
        await d.reports.update(rid, {"agent_fix": fix})
        await _audit(d, actor, rid, "apply", fix)
        return await verify(d, rid, actor)
    if fix.get("type") == "recollect" and fix.get("status") == "proposed":
        if not run_id:
            raise AppError("BAD_REQUEST", "재수집 실행 번호가 없습니다.")
        fix.update(
            status="recollecting", run_id=run_id[:80], applied_at=now, applied_by=actor.email
        )
        await d.reports.update(rid, {"agent_fix": fix})
        await _audit(d, actor, rid, "apply", fix)
        return fix
    raise AppError("BAD_REQUEST", "승인할 조치안이 없습니다.")


async def verify(d: FixDeps, rid: str, actor: Any) -> dict[str, Any]:
    """해결 판정. 자료가 실제로 바뀌었고 2/2 통과면 '수정 완료'로 자동 종결(되돌리기 가능)."""
    row = await _row(d, rid)
    fix = dict(row.get("agent_fix") or {})
    kind, status = fix.get("type"), fix.get("status")
    must = None
    if kind == "place":
        if status not in ("applied", "recheck_failed") or not fix.get("place_id"):
            raise AppError("BAD_REQUEST", "장소를 반영한 뒤 확인할 수 있습니다.")
        must = (fix.get("draft") or {}).get("location")
    elif kind == "recollect":
        run = await d.run_status(str(fix.get("run_id") or "")) if fix.get("run_id") else None
        if not run or run.get("status") != "success":
            raise AppError(
                "BAD_REQUEST", "재수집이 아직 끝나지 않았습니다. 수집 실행을 확인하세요."
            )
    elif kind != "unanswered" or status not in ("linked", "recheck_failed"):
        raise AppError("BAD_REQUEST", "확인할 조치가 없습니다.")
    question = (row.get("snapshot") or {}).get("question_masked") or ""
    result = await resolve.verify_fix(question, d.run_turn, d.paraphrase, must)
    now = datetime.now(UTC)
    fix.update(status="verified" if result["passed"] else "recheck_failed", check=result)
    change: dict[str, Any] = {"agent_fix": fix}
    if result["passed"] and row.get("status") != "resolved":
        change.update(
            {
                "status": "resolved",
                "reviewed_by": "agent-verify",
                "reviewed_at": now,
                "admin_note": "에이전트 재확인 2/2 통과로 종결",
                "undo": {
                    "action": "agent_resolve",
                    "after": "resolved",
                    "prev": {
                        "status": row.get("status"),
                        "reviewed_by": row.get("reviewed_by"),
                        "reviewed_at": row.get("reviewed_at"),
                        "admin_note": row.get("admin_note"),
                    },
                },
            }
        )
    await d.reports.update(rid, change)
    await _audit(d, actor, rid, "verify", fix)
    return fix

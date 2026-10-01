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
    # 초안 → 검수 완료 장소. {place_id, prev(반영 전 문서 또는 None), request_id}
    save_place: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
    run_status: Callable[[str], Awaitable[dict[str, Any] | None]]
    # 장소 반영 되돌리기(그 뒤 다른 수정이 없을 때만) — GPT5 #799-3
    restore_place: Callable[[str, dict[str, Any] | None, str], Awaitable[None]] | None = None
    # 제보 속 학교 주소를 [홈페이지 등록]하고 수집 실행(#919 P1): (urls) → (page_ids, run_id)
    register_pages: Callable[[list[str]], Awaitable[tuple[list[str], str]]] | None = None


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
    if kind == "register":
        urls = resolve.school_urls(row.get("text_masked") or "", snap.get("question_masked") or "")
        fix.update(status="proposed", urls=urls)
    elif kind == "quality":
        # 답변 품질(#919 P2): 평가셋 후보로 쌓고, 재확인은 미응답처럼 2/2(제보된 답 반복이면 실패)
        await d.reports.add_eval_candidate(
            rid,
            {
                "status": "candidate",
                "kind": "answer_quality",
                "created_at": datetime.now(UTC),
                "question_masked": snap.get("question_masked") or "",
                "reported_answer": (snap.get("answer_text") or "")[:1500],
                "complaint": (row.get("text_masked") or "")[:300],
            },
        )
        fix.update(status="linked")
    elif kind == "place":
        draft = await resolve.draft_place(
            snap.get("question_masked") or "", row.get("text_masked") or "", d.search, d.extract
        )
        fix.update(status="proposed" if draft["ok"] else "no_draft", draft=draft)
    elif kind == "recollect":
        pages = await resolve.compare_sources(snap, d.fetch_text)
        sources = sorted({p["source"] for p in pages if p["status"] == "changed" and p["source"]})
        if not any(p["status"] in ("same", "changed") for p in pages):
            # 비교할 웹 원문이 없음(예: 장소표만 인용) → 자료 부족으로 보고 미응답에 연결
            await d.reports.add_unanswered(snap.get("question_masked") or "")
            fix.update(type="unanswered", status="linked", pages=pages, note="no_source")
        else:
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
            # 승인 직전에 출처를 다시 읽어 인용문이 아직 있는지 확인(GPT5 #799-2)
            try:
                live = await d.fetch_text(str(draft.get("source_url") or ""))
            except Exception as exc:  # noqa: BLE001
                raise AppError(
                    "BAD_REQUEST", "출처 페이지를 다시 읽지 못했습니다. 잠시 뒤 다시 시도하세요."
                ) from exc
            if not resolve.quote_present(live, str(draft.get("quote") or "")):
                fix.update(status="stale_draft", note="출처 원문이 바뀌어 초안을 만료했습니다")
                await d.reports.update(rid, {"agent_fix": fix})
                raise AppError(
                    "BAD_REQUEST", "출처 원문이 바뀌었습니다. [조치안 다시 만들기]를 눌러 주세요."
                )
            saved = await d.save_place(draft)
            fix.update(
                place_id=saved["place_id"],
                place_prev=saved.get("prev"),
                place_request_id=saved.get("request_id"),
            )
        fix.update(status="applied", applied_at=now, applied_by=actor.email)
        await d.reports.update(rid, {"agent_fix": fix})
        await _audit(d, actor, rid, "apply", fix)
        return await verify(d, rid, actor)
    if fix.get("type") == "register" and fix.get("status") == "proposed":
        if d.register_pages is None:
            raise AppError("BAD_REQUEST", "홈페이지 등록을 할 수 없는 환경입니다.")
        page_ids, run = await d.register_pages(list(fix.get("urls") or []))
        fix.update(
            status="recollecting",
            run_id=run[:80],
            page_ids=page_ids,
            # 해결 판정에 쓰는 '바뀐 문서' 목록 — 새로 등록한 페이지를 인용해야 통과
            pages=[{"id": f"guide:web-{p}:1", "status": "changed", "source": "web_pages"} for p in page_ids],
            applied_at=now,
            applied_by=actor.email,
        )
        await d.reports.update(rid, {"agent_fix": fix})
        await _audit(d, actor, rid, "apply", fix)
        return fix
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
    required: list[str] | None = None
    if kind == "place":
        if status not in ("applied", "recheck_failed") or not fix.get("place_id"):
            raise AppError("BAD_REQUEST", "장소를 반영한 뒤 확인할 수 있습니다.")
        must = (fix.get("draft") or {}).get("location")
        required = [f"place:{fix['place_id']}"]  # 반영한 장소 자체를 인용해야(GPT5 #799-1)
    elif kind in ("recollect", "register"):
        run = await d.run_status(str(fix.get("run_id") or "")) if fix.get("run_id") else None
        if not run or run.get("status") != "success":
            raise AppError(
                "BAD_REQUEST", "재수집이 아직 끝나지 않았습니다. 수집 실행을 확인하세요."
            )
        required = sorted(
            {
                resolve.doc_prefix(p["id"])
                for p in fix.get("pages") or []
                if p.get("status") == "changed"
            }
        )
    elif kind not in ("unanswered", "quality") or status not in ("linked", "recheck_failed"):
        raise AppError("BAD_REQUEST", "확인할 조치가 없습니다.")
    snap = row.get("snapshot") or {}
    question = snap.get("question_masked") or ""
    result = await resolve.verify_fix(
        question, d.run_turn, d.paraphrase, must, snap.get("answer_text") or "", required
    )
    now = datetime.now(UTC)
    fix.update(status="verified" if result["passed"] else "recheck_failed", check=result)
    change: dict[str, Any] = {"agent_fix": fix}
    if result["passed"] and row.get("status") != "resolved":
        change.update(
            {
                "status": "resolved",
                "reviewed_by": "agent-verify",
                "reviewed_at": now,
                "admin_note": f"에이전트 재확인 2/2 통과로 종결 — {fixed_by(fix)}",
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


def fixed_by(fix: dict[str, Any]) -> str:
    """무엇으로 고쳐졌는지 한 줄(#919 P4) — 제보 카드·감사 로그에 남는다."""
    kind = fix.get("type")
    if kind == "register":
        return "홈페이지 등록·수집: " + ", ".join(fix.get("urls") or [])
    if kind == "recollect":
        return "원문 재수집: " + ", ".join(fix.get("source_ids") or [])
    if kind == "place":
        dr = fix.get("draft") or {}
        return f"장소표 반영: {dr.get('name', '')} {dr.get('location', '')}".strip()
    if kind == "quality":
        return "답변 품질 재확인(평가셋 등록)"
    return "자료 보강 뒤 재확인"


async def revert_place(d: FixDeps, rid: str, actor: Any) -> dict[str, Any]:
    """장소 반영 되돌리기(GPT5 #799-3): 반영 뒤 다른 수정이 없을 때만 반영 전 상태로.

    새로 만든 장소였으면 검수 대기(pending)로 돌려 학생에게 보이지 않게 한다.
    자동 종결된 제보도 함께 원래 상태로 돌린다.
    """
    row = await _row(d, rid)
    fix = dict(row.get("agent_fix") or {})
    if fix.get("type") != "place" or not fix.get("place_id") or d.restore_place is None:
        raise AppError("BAD_REQUEST", "되돌릴 장소 반영이 없습니다.")
    if fix.get("status") == "reverted":
        raise AppError("BAD_REQUEST", "이미 되돌렸습니다.")
    await d.restore_place(
        fix["place_id"], fix.get("place_prev"), str(fix.get("place_request_id") or "")
    )
    fix.update(status="reverted", reverted_at=datetime.now(UTC), reverted_by=actor.email)
    change: dict[str, Any] = {"agent_fix": fix}
    undo = row.get("undo") or {}
    if undo.get("action") == "agent_resolve" and row.get("status") == "resolved":
        change.update({**(undo.get("prev") or {}), "undo": None})
    await d.reports.update(rid, change)
    await _audit(d, actor, rid, "revert_place", fix)
    return fix


CLOSED = ("resolved", "rejected", "no_issue")


async def auto_step(d: FixDeps, rid: str, actor: Any) -> dict[str, Any]:
    """사람 승인이 필요 없는 다음 단계 하나를 자동으로(교수님 2026-10-01: 일괄 재확인·자동 처리).

    - 조치안이 없으면 분석(analyze) — 자료를 바꾸지 않는다
    - 반영됐거나 미응답에 연결된 조치는 재확인(verify) — 2/2 통과면 자동 종결(되돌리기 가능)
    - 조치안이 '승인 대기'(proposed)면 건너뛴다: 공식 자료 변경은 사람 승인 1회 뒤에만
    """
    row = await d.reports.get(rid)
    if not row or row.get("type") != "wrong_info":
        return {"id": rid, "action": "skip", "reason": "not_wrong_info"}
    if row.get("status") in CLOSED:
        return {"id": rid, "action": "skip", "reason": "closed"}
    fix = row.get("agent_fix") or {}
    kind, status = fix.get("type"), fix.get("status")
    if not fix:
        out = await analyze(d, rid, actor)
        return {"id": rid, "action": "analyze", "status": out.get("status")}
    verifiable = (
        (kind in ("unanswered", "quality") and status in ("linked", "recheck_failed"))
        or (kind == "place" and status in ("applied", "recheck_failed") and fix.get("place_id"))
        or (kind in ("recollect", "register") and status == "recollecting")
    )
    if verifiable:
        try:
            out = await verify(d, rid, actor)
        except AppError as exc:  # 재수집 미완료 등 — 다음에 다시
            return {"id": rid, "action": "wait", "reason": exc.message}
        return {"id": rid, "action": "verify", "status": out.get("status")}
    return {
        "id": rid,
        "action": "skip",
        "status": status,
        "needs_human": status == "proposed",
    }

# ruff: noqa: E501 — 설명 문장은 줄바꿈하지 않는다
"""매일 밤 평가셋 재실행 — '사용하면 할수록 더 똑똑해지는 챗봇' 실증(교수님 #796·#797).

- 평가셋(eval_set)은 저절로 쌓인다: 기존 20문항 + 답하지 못한 질문(unanswered) + 제보된 질문(reports).
  질문은 모두 개인정보를 가린 문장(query_masked)만 쓴다.
- 매일 같은 묶음을 새 대화로 다시 실행하고, 제보함과 같은 엄격 기준으로 판정한다:
  답변 + 인용 있음, 원문 충돌 없음, '찾지 못했습니다'류 아님, 제보된(틀렸다는) 답의 반복 아님.
- 핵심 지표: "예전에 답하지 못했던 질문 중 지금 답하는 비율"
  (미응답·제보 출신이거나 처음 실행에서 실패했던 질문을 분모로).
- 결과는 eval_runs/{날짜}(KST)에 남고 관리자 통계에 곡선으로 보인다.

실행: python -m backend.app.nightly_eval   (Cloud Run Job campusbridge-eval, 매일 03:30 KST)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from backend.reports.resolve import run_ok

log = logging.getLogger("campusbridge.eval")
KST = timezone(timedelta(hours=9), "KST")
SEED = Path(__file__).resolve().parents[1] / "data" / "eval_seed.jsonl"
MAX_ITEMS = 150
CONCURRENCY = 3
HISTORY_KEEP = 30


def item_id(query: str) -> str:
    return hashlib.sha1(" ".join(query.lower().split()).encode()).hexdigest()[:20]


def seed_items(path: Path = SEED) -> list[dict[str, Any]]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            q = json.loads(line).get("question")
            if q:
                out.append({"query_masked": q, "source": "seed", "reported_answer": ""})
    return out


def was_failing(item: dict[str, Any]) -> bool:
    """분모: 예전에 답하지 못했던 질문(미응답·제보 출신, 또는 첫 실행에서 실패)."""
    return item.get("source") in ("unanswered", "report") or item.get("first_ok") is False


def summarize(items: list[dict[str, Any]], day: str, version: str) -> dict[str, Any]:
    ran = [i for i in items if i.get("last_day") == day]
    failing = [i for i in ran if was_failing(i)]
    fixed = [i for i in failing if i.get("last_ok")]
    by_source: dict[str, dict[str, int]] = {}
    for i in ran:
        s = by_source.setdefault(i["source"], {"total": 0, "ok": 0})
        s["total"] += 1
        s["ok"] += int(bool(i.get("last_ok")))
    return {
        "day": day,
        "app_version": version,
        "total": len(ran),
        "ok": sum(bool(i.get("last_ok")) for i in ran),
        "was_failing": len(failing),
        "now_ok": len(fixed),
        "recovered_rate": round(len(fixed) / len(failing), 3) if failing else None,
        "by_source": by_source,
        "created_at": datetime.now(UTC),
    }


class FirestoreEval:
    def __init__(self, project: str, database: str) -> None:
        from google.cloud import firestore

        self.db = firestore.AsyncClient(project=project, database=database)

    async def collect(self) -> list[dict[str, Any]]:
        """평가셋에 새 질문을 더하고 전체를 돌려준다(이미 있는 질문은 출처·첫 결과를 유지)."""
        col = self.db.collection("eval_set")
        have = {d.id: d.to_dict() or {} async for d in col.stream()}
        new: list[dict[str, Any]] = list(seed_items())
        async for d in self.db.collection("unanswered").stream():
            r = d.to_dict() or {}
            if r.get("query_masked") and r.get("fallback_reason") != "out_of_scope":
                new.append(
                    {
                        "query_masked": r["query_masked"],
                        "source": "unanswered",
                        "reported_answer": "",
                    }
                )
        async for d in self.db.collection("reports").where("type", "==", "wrong_info").stream():
            snap = (d.to_dict() or {}).get("snapshot") or {}
            if snap.get("question_masked"):
                new.append(
                    {
                        "query_masked": snap["question_masked"],
                        "source": "report",
                        "reported_answer": snap.get("answer_text") or "",
                    }
                )
        now = datetime.now(UTC)
        for it in new:
            iid = item_id(it["query_masked"])
            if iid not in have:
                row = {**it, "added_at": now}
                await col.document(iid).set(row)
                have[iid] = row
        return [{"id": k, **v} for k, v in have.items()]

    async def save_item(self, iid: str, fields: dict[str, Any]) -> None:
        await self.db.collection("eval_set").document(iid).set(fields, merge=True)

    async def save_run(self, summary: dict[str, Any]) -> None:
        await self.db.collection("eval_runs").document(summary["day"]).set(summary)


async def run_all(store: Any, run_turn: Any, version: str, day: str) -> dict[str, Any]:
    items = await store.collect()
    items.sort(key=lambda i: (i["source"] != "seed", str(i.get("added_at"))))  # 오래된 것부터
    todo = items[:MAX_ITEMS]
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(it: dict[str, Any]) -> None:
        async with sem:
            try:
                final = await run_turn(it["query_masked"])
                ok, why = run_ok(final, None, it.get("reported_answer") or "")
            except Exception as exc:  # noqa: BLE001 — 한 문항 실패가 전체를 막지 않게
                ok, why = False, type(exc).__name__
            hist = dict(it.get("history") or {})
            hist[day] = ok
            hist = dict(sorted(hist.items())[-HISTORY_KEEP:])
            fields = {"last_ok": ok, "last_reason": why, "last_day": day, "history": hist}
            if "first_ok" not in it:
                fields["first_ok"] = ok
            it.update(fields)
            await store.save_item(it["id"], fields)

    await asyncio.gather(*(one(it) for it in todo))
    summary = summarize(todo, day, version)
    await store.save_run(summary)
    return summary


def main() -> int:
    import os
    import time

    from backend.agent.graph import build_graph
    from backend.app.config import get_settings
    from backend.app.main import default_deps, recheck_state

    logging.basicConfig(level=logging.INFO)
    s = get_settings()
    graph = build_graph(default_deps(s))
    store = FirestoreEval(os.environ["GCP_PROJECT_ID"], s.firestore_db)

    async def run_turn(q: str) -> dict[str, Any]:
        return await graph.ainvoke(recheck_state(q, s))

    t0 = time.monotonic()
    day = datetime.now(KST).date().isoformat()
    summary = asyncio.run(run_all(store, run_turn, s.app_version, day))
    log.info(
        json.dumps(
            {
                "event": "nightly_eval",
                **{k: v for k, v in summary.items() if k != "created_at"},
                "elapsed_s": round(time.monotonic() - t0),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

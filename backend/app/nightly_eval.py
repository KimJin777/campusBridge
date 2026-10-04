# ruff: noqa: E501 — 설명 문장은 줄바꿈하지 않는다
"""매일 밤 평가셋 재실행 — '사용하면 할수록 더 똑똑해지는 챗봇' 실증(교수님 #796·#797).

- 평가셋(eval_set)은 저절로 쌓인다: 기존 20문항 + 답하지 못한 질문(unanswered) + 제보된 질문(reports).
  질문은 모두 개인정보를 가린 문장(query_masked)만 쓴다.
- 매일 같은 묶음을 새 대화로 다시 실행하고, 제보함과 같은 엄격 기준으로 판정한다:
  답변 + 인용 있음, 원문 충돌 없음, '찾지 못했습니다'류 아님, 제보된(틀렸다는) 답의 반복 아님.
- 질문마다 의미가 같은 다른 표현(패러프레이즈)을 한 번 만들어 고정하고, 원 질문과 둘 다 통과해야
  통과로 센다(특정 문장 암기 방지 — GPT5 #800·Gemini #803).
- 핵심 지표: "예전에 답하지 못했던 질문 중 지금 답하는 비율"(회복률).
  첫 실행일 기준 **고정 코호트**로도 따로 계산해 날짜별 모집단이 바뀌어도 비교할 수 있게 하고,
  **회귀율**(통과했다가 실패로 바뀐 비율), 직전 실행 대비 **전이표**, 분자/분모와 **95% 신뢰구간**을 함께 남긴다.
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
CONCURRENCY = (
    1  # 3이면 분당 호출 한도(429)에 걸려 '가짜 회귀'가 났다(2026-10-02 03:30, 게시판 #1027)
)
GAP_SEC = 1.0  # 문항 사이 간격
QUOTA_RETRIES = 3  # 429가 섞여 실패한 문항은 2·4·8초 뒤 다시 잰다
QUOTA_BACKOFF = 2.0
QUOTA_EXCEEDED = "quota_exceeded"  # 끝내 429 → '측정 불가'(통과·실패 어느 쪽에도 넣지 않음)
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


def wilson(k: int, n: int, z: float = 1.96) -> list[float] | None:
    """비율의 95% 신뢰구간(Wilson). 표본이 작을 때 착시를 막는다(GPT5 #800)."""
    if n <= 0:
        return None
    p = k / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / den
    return [round(max(0.0, mid - half), 3), round(min(1.0, mid + half), 3)]


def _rate(k: int, n: int) -> dict[str, Any]:
    return {"k": k, "n": n, "rate": round(k / n, 3) if n else None, "ci95": wilson(k, n)}


def _prev(item: dict[str, Any], day: str) -> bool | None:
    before = [(d, v) for d, v in (item.get("history") or {}).items() if d < day]
    return sorted(before)[-1][1] if before else None


def summarize(
    items: list[dict[str, Any]],
    day: str,
    version: str,
    baseline_day: str,
    run_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """run_meta: 모델·프롬프트·색인 버전(GPT5 #1460). 같은 값끼리의 날만 전후 비교의 주 증거로 쓴다."""
    ran = [i for i in items if i.get("last_day") == day and i.get("last_reason") != QUOTA_EXCEEDED]
    unmeasured = sum(
        1 for i in items if i.get("last_day") == day and i.get("last_reason") == QUOTA_EXCEEDED
    )
    failing = [i for i in ran if was_failing(i)]
    frozen = [i for i in ran if i.get("cohort") == baseline_day]
    f_fail = [i for i in frozen if was_failing(i)]
    f_pass = [i for i in frozen if i.get("first_ok") is True and i.get("source") == "seed"]
    trans = {"fail_to_ok": 0, "ok_to_fail": 0, "same_ok": 0, "same_fail": 0, "new": 0}
    for i in ran:
        prev, now = _prev(i, day), bool(i.get("last_ok"))
        key = (
            "new"
            if prev is None
            else (
                "fail_to_ok"
                if not prev and now
                else "ok_to_fail"
                if prev and not now
                else "same_ok"
                if now
                else "same_fail"
            )
        )
        trans[key] += 1
    by_source: dict[str, dict[str, int]] = {}
    for i in ran:
        s_ = by_source.setdefault(i["source"], {"total": 0, "ok": 0})
        s_["total"] += 1
        s_["ok"] += int(bool(i.get("last_ok")))
    recovered = _rate(sum(bool(i.get("last_ok")) for i in failing), len(failing))
    return {
        "day": day,
        "app_version": version,
        "baseline_day": baseline_day,
        "total": len(ran),
        "unmeasured": unmeasured,
        "ok": sum(bool(i.get("last_ok")) for i in ran),
        "strict_pass": _rate(sum(bool(i.get("last_ok")) for i in ran), len(ran)),
        "was_failing": len(failing),
        "now_ok": recovered["k"],
        "recovered_rate": recovered["rate"],
        "recovered": recovered,
        # 고정 코호트(첫 실행일에 있던 질문만) — 날짜별로 같은 모집단 비교
        "frozen_recovered": _rate(sum(bool(i.get("last_ok")) for i in f_fail), len(f_fail)),
        "frozen_regression": _rate(sum(not i.get("last_ok") for i in f_pass), len(f_pass)),
        "transitions": trans,
        "by_source": by_source,
        "run_meta": dict(run_meta or {}),
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

    async def index_snapshot(self) -> dict[str, Any] | None:
        """최근 수집 실행 5건 — 그날 평가가 어떤 색인 위에서 돌았는지 남긴다(GPT5 #1460)."""
        from google.cloud import firestore

        try:
            q = (
                self.db.collection("ingestion_runs")
                .order_by("created_at", direction=firestore.Query.DESCENDING)
                .limit(5)
            )
            runs = [
                f"{d.id}:{(d.to_dict() or {}).get('status')}:{(d.to_dict() or {}).get('finished_at')}"
                async for d in q.stream()
            ]
        except Exception:  # noqa: BLE001 — 평가는 계속 돌린다
            return None
        return {"runs": runs, "hash": hashlib.sha256("|".join(runs).encode()).hexdigest()[:16]}

    async def baseline_day(self) -> str | None:
        days = [d.id async for d in self.db.collection("eval_runs").stream()]
        return min(days) if days else None


async def run_all(
    store: Any,
    run_turn: Any,
    paraphrase: Any,
    version: str,
    day: str,
    *,
    quota_hits: Any = None,
    sleep: Any = asyncio.sleep,
    run_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """quota_hits: 지금까지의 429 횟수를 돌려주는 함수(문항 전후 차이로 그 문항이 한도에 걸렸는지 본다).

    CONCURRENCY=1(차례 실행)이라 전후 차이가 그 문항 몫이다.
    """
    baseline = await store.baseline_day() or day
    items = await store.collect()
    items.sort(key=lambda i: (i["source"] != "seed", str(i.get("added_at"))))  # 오래된 것부터
    todo = items[:MAX_ITEMS]
    sem = asyncio.Semaphore(CONCURRENCY)

    async def measure(it: dict[str, Any], para: str) -> tuple[bool, str]:
        results = []
        for q in (it["query_masked"], para):
            try:
                final = await run_turn(q)
                results.append(run_ok(final, None, it.get("reported_answer") or ""))
            except Exception as exc:  # noqa: BLE001 — 한 문항 실패가 전체를 막지 않게
                results.append((False, type(exc).__name__))
        ok = all(r[0] for r in results)
        return ok, ("ok" if ok else next(r[1] for r in results if not r[0]))

    async def one(it: dict[str, Any]) -> None:
        async with sem:
            fields: dict[str, Any] = {}
            para = it.get("paraphrase")
            if not para:  # 한 번 만들어 고정(매일 바뀌면 비교가 안 된다)
                try:
                    para = await paraphrase(it["query_masked"])
                except Exception:  # noqa: BLE001
                    para = None
                para = para or f"{it['query_masked'].rstrip('?？ ')} 알려 주세요"
                fields["paraphrase"] = para
            for attempt in range(QUOTA_RETRIES + 1):
                before = quota_hits() if quota_hits else 0
                ok, why = await measure(it, para)
                hit = bool(quota_hits) and quota_hits() > before
                if ok or not hit:
                    break
                if attempt < QUOTA_RETRIES:
                    await sleep(QUOTA_BACKOFF * 2**attempt)
            else:
                ok, why = False, QUOTA_EXCEEDED
            if why == QUOTA_EXCEEDED:  # 측정 불가: 통과 이력·첫 결과를 건드리지 않는다
                fields.update({"last_reason": why, "last_day": day})
            else:
                hist = dict(it.get("history") or {})
                hist[day] = ok
                hist = dict(sorted(hist.items())[-HISTORY_KEEP:])
                fields.update({"last_ok": ok, "last_reason": why, "last_day": day, "history": hist})
                if "first_ok" not in it:
                    fields.update(first_ok=ok, cohort=day)
            it.update(fields)
            await store.save_item(it["id"], fields)
            await sleep(GAP_SEC)

    await asyncio.gather(*(one(it) for it in todo))
    summary = summarize(todo, day, version, baseline, run_meta)
    await store.save_run(summary)
    return summary


class QuotaCounter(logging.Handler):
    """campusbridge.clients의 external_call 로그에서 429로 끝난 호출을 센다(공용 호출 코드는 그대로 둔다)."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.hits = 0

    def emit(self, record: logging.LogRecord) -> None:
        if '"result": "status:429"' in record.getMessage():
            self.hits += 1


def main() -> int:
    import os
    import time

    from backend.agent.graph import build_graph
    from backend.app.clients import deadline_after
    from backend.app.config import get_settings
    from backend.app.main import default_deps, recheck_state
    from backend.reports import resolve

    logging.basicConfig(level=logging.INFO)
    s = get_settings()
    deps = default_deps(s)
    graph = build_graph(deps)
    store = FirestoreEval(os.environ["GCP_PROJECT_ID"], s.firestore_db)

    async def run_turn(q: str) -> dict[str, Any]:
        return await graph.ainvoke(recheck_state(q, s))

    async def paraphrase(q: str) -> str | None:
        got = await deps.llm.structured(
            resolve.Paraphrase,
            resolve.PARAPHRASE_PROMPT,
            q,
            node="eval_paraphrase",
            deadline=deadline_after(8000),
        )
        return got.text

    t0 = time.monotonic()
    day = datetime.now(KST).date().isoformat()
    quota = QuotaCounter()
    logging.getLogger("campusbridge.clients").addHandler(quota)
    from backend.agent import prompts

    prompt_hash = hashlib.sha256(Path(prompts.__file__).read_bytes()).hexdigest()[:16]

    async def go() -> dict[str, Any]:
        meta = {
            "model_id": s.gemini_model,
            "fallback_model_id": s.gemini_fallback_model,
            "prompt_hash": prompt_hash,
            "index_snapshot": await store.index_snapshot(),
        }
        return await run_all(
            store,
            run_turn,
            paraphrase,
            s.app_version,
            day,
            quota_hits=lambda: quota.hits,
            run_meta=meta,
        )

    summary = asyncio.run(go())
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

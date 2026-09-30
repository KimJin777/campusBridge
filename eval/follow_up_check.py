"""후속 질문 기본 목록 회귀 점검(GPT5 #675 — 교수님 #676 지시).

    uv run python -m eval.follow_up_check

TOPIC_DEFAULTS의 고정 질문을 실제 그래프로 한 번씩 돌려, 답변·되묻기가 아닌(fallback 등) 질문을
backend/data/follow_up_coverage.json의 failed에 적는다. pick_follow_ups는 이 목록을 빼고 채운다.
배포 전 또는 색인 갱신 후 다시 실행한다.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from typing import Any

from backend.agent.follow_ups import COVERAGE_PATH, TOPIC_DEFAULTS
from eval.run_eval import _commit, run_item

OK_OUTCOMES = ("answer", "ask")  # ask = 학과 되묻기(졸업) — 정상 흐름


async def check() -> dict[str, Any]:
    from backend.agent.graph import build_graph
    from backend.app.config import get_settings
    from backend.app.main import default_deps

    graph = build_graph(default_deps(get_settings()))
    questions = [q for _keys, qs in TOPIC_DEFAULTS for q in qs]
    sem = asyncio.Semaphore(3)

    async def one(q: str) -> tuple[str, str]:
        async with sem:
            run = await run_item(graph, {"id": q, "question": q})
        return q, str(run["turns"][-1].get("outcome"))

    results = dict(await asyncio.gather(*(one(q) for q in questions)))
    return {
        "checked_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "commit": _commit(),
        "outcomes": results,
        "failed": sorted(q for q, o in results.items() if o not in OK_OUTCOMES),
    }


def main() -> int:
    report = asyncio.run(check())
    COVERAGE_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"{len(report['outcomes'])}문항 중 제외 {len(report['failed'])}: {report['failed']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

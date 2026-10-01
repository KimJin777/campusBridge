# ruff: noqa: E501
"""매일 밤 평가셋 재실행(교수님 #796·#797) — '예전에 못 답한 질문 중 지금 답하는 비율'."""

from types import SimpleNamespace

import pytest

from backend.app import nightly_eval as ne


def ans(text):
    return {
        "outcome": "answer",
        "answer": SimpleNamespace(sentences=[SimpleNamespace(text=text)], cited=["x"], notices=[]),
    }


class FakeStore:
    def __init__(self, items):
        self.items = items
        self.saved = {}
        self.runs = []

    async def collect(self):
        return self.items

    async def save_item(self, iid, fields):
        self.saved.setdefault(iid, {}).update(fields)

    async def save_run(self, summary):
        self.runs.append(summary)

    async def baseline_day(self):
        return self.runs[0]["day"] if self.runs else None


def test_seed_file_is_packaged_and_ids_stable():
    seeds = ne.seed_items()
    assert len(seeds) == 20 and all(s["source"] == "seed" for s in seeds)
    assert ne.item_id("휴학  방법") == ne.item_id("휴학 방법")


@pytest.mark.asyncio
async def test_recovered_rate_counts_only_previously_failing_questions():
    items = [
        {"id": "a", "query_masked": "통학버스 노선", "source": "unanswered"},
        {
            "id": "b",
            "query_masked": "학사관리팀 어디",
            "source": "report",
            "reported_answer": "교통안전관리 규정에 따르면 적용됩니다",
        },
        {"id": "c", "query_masked": "휴학 방법", "source": "seed", "first_ok": True},
        {"id": "d", "query_masked": "장학 기간", "source": "seed", "first_ok": False},
    ]
    answers = {
        "통학버스 노선": ans("1호차는 마산역에서 07:40에 출발합니다."),
        "학사관리팀 어디": ans("교통안전관리 규정에 따르면 적용됩니다"),  # 제보된 답 반복 → 실패
        "휴학 방법": ans("휴학은 학사관리팀에 신청합니다."),
        "장학 기간": ans("관련 공지에서 기간을 찾지 못했습니다."),  # '찾지 못함' → 실패
    }

    async def run_turn(q):
        return answers[q.removesuffix(" (다른 표현)")]

    async def paraphrase(q):
        return q + " (다른 표현)"

    store = FakeStore(items)
    s = await ne.run_all(store, run_turn, paraphrase, "0.27.0", "2026-10-01")
    assert (s["total"], s["ok"], s["was_failing"], s["now_ok"]) == (4, 2, 3, 1)
    assert s["recovered_rate"] == round(1 / 3, 3)
    assert (
        store.saved["a"]["history"] == {"2026-10-01": True} and store.saved["a"]["first_ok"] is True
    )
    assert "first_ok" not in store.saved["c"]  # 첫 결과는 한 번만 기록
    assert store.runs[0]["by_source"]["seed"] == {"total": 2, "ok": 1}


@pytest.mark.asyncio
async def test_paraphrase_must_also_pass_and_frozen_cohort_tracks_regression():
    """패러프레이즈도 통과해야 하고(암기 방지), 고정 코호트로 회복·회귀를 따로 센다."""
    items = [
        {"id": "a", "query_masked": "통학버스 노선", "source": "unanswered"},
        {"id": "c", "query_masked": "휴학 방법", "source": "seed"},
    ]
    day1 = {
        "통학버스 노선": ans("1호차는 마산역 07:40"),
        "통학버스 노선 다른": ans("찾지 못했습니다"),
        "휴학 방법": ans("휴학은 학사관리팀"),
        "휴학 방법 다른": ans("휴학은 학사관리팀"),
    }

    async def para(q):
        return q + " 다른"

    store = FakeStore(items)

    async def t1(q):
        return day1[q]

    s1 = await ne.run_all(store, t1, para, "v", "2026-10-01")
    assert (
        store.saved["a"]["last_ok"] is False
        and store.saved["a"]["last_reason"] == "답변이 '찾지 못함'"
    )
    assert store.saved["a"]["cohort"] == "2026-10-01" and s1["frozen_recovered"]["n"] == 1
    day2 = {
        **day1,
        "통학버스 노선 다른": ans("1호차는 마산역 07:40"),
        "휴학 방법 다른": ans("관련 내용을 찾지 못했습니다"),
    }

    async def t2(q):
        return day2[q]

    s2 = await ne.run_all(store, t2, para, "v", "2026-10-02")
    assert s2["baseline_day"] == "2026-10-01"
    assert s2["frozen_recovered"]["k"] == 1 and s2["frozen_regression"]["k"] == 1
    assert s2["transitions"]["fail_to_ok"] == 1 and s2["transitions"]["ok_to_fail"] == 1
    assert s2["frozen_recovered"]["ci95"][0] < 1.0


def test_wilson_interval_is_wide_for_small_samples():
    lo, hi = ne.wilson(4, 5)
    assert lo < 0.5 < 0.8 < hi and ne.wilson(0, 0) is None

from backend.agent.follow_ups import next_recent, pick_follow_ups


def test_filters_repeats_long_and_personal_then_fills_by_topic():
    out = pick_follow_ups(
        [
            "휴학하려면 어떻게 해요?",
            "학번을 알려주면 확인해 드릴까요?",
            "가" * 50,
            "복학 신청은 언제 하나요",
        ],
        asked=["휴학하려면 어떻게 해요"],
        topic="휴학",
    )
    assert out[0] == "복학 신청은 언제 하나요"  # 모델 제안 중 살아남은 것 먼저
    assert "휴학하려면 어떻게 해요?" not in out  # 이미 물은 질문
    assert all("학번" not in q for q in out) and all(len(q) <= 40 for q in out)
    assert len(out) == 3 and "복학 신청은 언제 하나요?" not in out  # 기본 질문으로 채우되 중복 없음


def test_no_topic_no_fill_and_recent_window():
    assert pick_follow_ups([], asked=[], topic="기타") == []
    assert next_recent(["a", "b", "c"], "d") == ["b", "c", "d"]
    assert next_recent([], None) == []


def test_defaults_skip_questions_failed_in_coverage_check(monkeypatch):
    """회귀 점검에서 답을 못 한 기본 질문은 칩으로 내지 않는다(GPT5 #675)."""
    from backend.agent import follow_ups

    monkeypatch.setattr(
        follow_ups, "failed_defaults", lambda: frozenset({"군휴학 서류는 뭐가 필요해요?"})
    )
    out = follow_ups.pick_follow_ups([], asked=[], topic="휴학")
    assert "군휴학 서류는 뭐가 필요해요?" not in out and len(out) == 2

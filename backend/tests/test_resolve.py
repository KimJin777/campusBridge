from backend.agent.resolve import resolve
from backend.domain import Evidence


def cal(start="2026-09-01", end="2026-09-30", **meta):
    m = {"semester": "2026-2", "topic": "휴학", "start": start, "end": end} | meta
    return Evidence(
        id="cal:1", kind="calendar", title="2026-2학기 휴학 신청", text="휴학 신청 9.1~9.30", meta=m
    )


def notice(
    title="2026-2학기 휴학 신청 기간 연장 안내",
    start="2026-09-01",
    end="2026-10-10",
    text="휴학 신청 기간을 연장합니다.",
    **meta,
):
    m = {
        "semester": "2026-2",
        "topic": "휴학",
        "start": start,
        "end": end,
        "published_at": "2026-09-25T09:00:00+09:00",
    } | meta
    return Evidence(
        id="notice:academic:9",
        kind="notice",
        title=title,
        text=text,
        meta=m,
    )


ART = Evidence(id="101_main_32", kind="article", title="학칙 제32조", text="휴학은 …")
GUIDE = Evidence(id="guide:4390:1", kind="guide", title="휴학 안내", text="…")


def test_adoption_by_fact_type():
    r = resolve(
        [ART, GUIDE, cal()], ["eligibility_or_limit", "procedure_and_contact", "current_deadline"]
    )
    assert r.adopted == {
        "eligibility_or_limit": ["101_main_32"],
        "procedure_and_contact": ["guide:4390:1"],
        "current_deadline": ["cal:1"],
    }
    assert r.supporting["eligibility_or_limit"] == ["guide:4390:1"]
    assert r.supporting["current_deadline"] == ["101_main_32"]
    assert r.conflicts == [] and r.template_sentences == []


def test_later_notice_with_change_marker_wins():
    r = resolve([cal(), notice()], ["current_deadline"])
    assert r.adopted["current_deadline"][0] == "notice:academic:9"
    assert r.conflicts[0].type == "notice_overrides_calendar"
    assert "공지 기준" in r.template_sentences[0]
    assert r.review_flags[0].public_action == "conflict_notice"


def test_date_mismatch_without_marker_needs_review():
    r = resolve(
        [cal(), notice(title="2026-2학기 휴학 신청 안내", text="휴학 신청 기간을 안내합니다.")],
        ["current_deadline"],
    )
    assert r.conflicts[0].type == "date_mismatch" and r.conflicts[0].needs_review
    assert "두 출처를 모두 확인" in r.template_sentences[0]
    assert r.review_flags[0].severity == "high"


def test_marker_but_older_notice_is_not_override():
    r = resolve([cal(published_at="2026-09-26T00:00:00+09:00"), notice()], ["current_deadline"])
    assert r.conflicts[0].type == "date_mismatch"


def test_no_comparison_across_semester_or_topic_or_same_dates():
    assert resolve([cal(), notice(semester="2026-1")], ["current_deadline"]).conflicts == []
    assert resolve([cal(), notice(topic="복학")], ["current_deadline"]).conflicts == []
    assert resolve([cal(), notice(end="2026-09-30")], ["current_deadline"]).conflicts == []
    assert resolve([cal(), notice(start=None, end=None)], ["current_deadline"]).conflicts == []

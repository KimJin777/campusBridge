# ruff: noqa: E501
"""제보함 에이전트 조치(교수님 #786, GPT5 #792 기준)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from backend.domain import AppError
from backend.reports import agent_fix, resolve
from backend.reports.store import MemoryReportStore

ACTOR = SimpleNamespace(email="admin@kyungnam.ac.kr", subject="1")
SRC = "https://www.kyungnam.ac.kr/ko/4319/subview.do"
EV = {
    "id": "guide:web-abc:1",
    "title": "반도체부트캠프사업단",
    "url": "https://www.kusemicamp.com/about",
    "text": "반도체부트캠프사업단은 창조관 3층 301호에 있습니다. 문의 055-249-1234",
}


def ans(text, cited=("x",), notices=()):
    return {
        "outcome": "answer",
        "answer": SimpleNamespace(
            sentences=[SimpleNamespace(text=text)], cited=list(cited), notices=list(notices)
        ),
    }


def deps(store, *, drafted=None, finals=None, page="", runs=None, placed=None, restored=None):
    finals = list(finals or [])

    async def fetch_text(url):
        return page

    async def search(q):
        return [EV]

    async def extract(user):
        assert "학번" not in user  # 제보 글은 프롬프트에 넣지 않는다
        return drafted

    async def run_turn(q):
        return finals.pop(0)

    async def paraphrase(q):
        return q + " 궁금해요"

    async def save_place(draft):
        placed.append(draft)
        return {"place_id": "rpt-1", "prev": None, "request_id": "req-1"}

    async def restore_place(pid, prev, expected):
        restored.append((pid, prev, expected))

    async def run_status(rid):
        return (runs or {}).get(rid)

    return agent_fix.FixDeps(
        store,
        fetch_text,
        search,
        extract,
        run_turn,
        paraphrase,
        save_place,
        run_status,
        restore_place,
    )


def report(
    store, question, kind="other", text="학번 20231234 위치가 틀려요", cards=(), quotes=None
):
    store.rows["rep00001"] = {
        "type": "wrong_info",
        "status": "pending",
        "kind": kind,
        "text_masked": text,
        "snapshot": {
            "question_masked": question,
            "cards": list(cards),
            "cited_quotes": quotes or {},
        },
    }
    return "rep00001"


def test_fix_type_and_place_name():
    assert (
        resolve.fix_type({"snapshot": {"question_masked": "반도체부트캠프사업단이 어디있나요?"}})
        == "place"
    )
    assert (
        resolve.fix_type({"kind": "stale_source", "snapshot": {"question_masked": "통학버스 노선"}})
        == "recollect"
    )
    assert (
        resolve.fix_type({"snapshot": {"question_masked": "통학버스 아무나 타나요?"}})
        == "unanswered"
    )
    assert resolve.place_name("반도체부트캠프사업단이 어디있나요?") == "반도체부트캠프사업단"
    assert (
        resolve.source_of("guide:web-1:2") == "web_pages"
        and resolve.source_of("196_main_30") == "rules"
    )


def test_draft_rejects_values_not_in_official_quote():
    good = resolve.PlaceDraftOut(
        found=True,
        name="반도체부트캠프사업단",
        location="창조관 3층 301호",
        phone="055-249-1234",
        evidence_id=EV["id"],
        quote="반도체부트캠프사업단은 창조관 3층 301호에 있습니다. 문의 055-249-1234",
    )
    assert resolve.check_draft(good, {EV["id"]: EV}, "") is None
    fake_loc = good.model_copy(update={"location": "한마관 1층"})
    assert (
        resolve.check_draft(fake_loc, {EV["id"]: EV}, "한마관 1층이에요")
        == "위치가 인용문에 없습니다"
    )
    fake_quote = good.model_copy(update={"quote": "사업단은 한마관 1층에 있습니다"})
    assert resolve.check_draft(fake_quote, {EV["id"]: EV}, "") == "인용문이 원문에 없습니다"
    bad_url = {EV["id"]: {**EV, "url": "https://evil.example.com"}}
    assert resolve.check_draft(good, bad_url, "") == "근거가 학교 원문이 아닙니다"


@pytest.mark.asyncio
async def test_place_flow_apply_then_auto_resolve_2_of_2():
    store = MemoryReportStore()
    rid = report(store, "반도체부트캠프사업단이 어디있나요?")
    drafted = resolve.PlaceDraftOut(
        found=True,
        name="반도체부트캠프사업단",
        location="창조관 3층 301호",
        evidence_id=EV["id"],
        quote="반도체부트캠프사업단은 창조관 3층 301호에 있습니다.",
    )
    placed, restored = [], []
    good = ans("반도체부트캠프사업단은 창조관 3층 301호에 있습니다.", cited=["place:rpt-1"])
    d = deps(
        store,
        drafted=drafted,
        finals=[good, good],
        placed=placed,
        page=EV["text"],
        restored=restored,
    )
    fix = await agent_fix.analyze(d, rid, ACTOR)
    assert (
        fix["type"] == "place"
        and fix["status"] == "proposed"
        and fix["draft"]["source_url"].startswith("https://")
    )
    assert store.rows[rid]["status"] == "pending"  # 분석만으로는 아무것도 바뀌지 않는다
    out = await agent_fix.apply(d, rid, ACTOR)
    assert placed and out["status"] == "verified" and all(r["ok"] for r in out["check"]["runs"])
    assert (
        store.rows[rid]["status"] == "resolved"
        and store.rows[rid]["undo"]["prev"]["status"] == "pending"
    )
    assert [a["action"] for a in store.audits] == [
        "report.agent.analyze",
        "report.agent.apply",
        "report.agent.verify",
    ]
    # 장소 반영 되돌리기 → 장소 원상복구 + 자동 종결도 되돌림(GPT5 #799-3)
    out = await agent_fix.revert_place(d, rid, ACTOR)
    assert out["status"] == "reverted" and restored == [("rpt-1", None, "req-1")]
    assert store.rows[rid]["status"] == "pending"


@pytest.mark.asyncio
async def test_place_verify_fails_if_answer_lacks_approved_location():
    store = MemoryReportStore()
    rid = report(store, "반도체부트캠프사업단이 어디있나요?")
    drafted = resolve.PlaceDraftOut(
        found=True,
        name="반도체부트캠프사업단",
        location="창조관 3층 301호",
        evidence_id=EV["id"],
        quote="반도체부트캠프사업단은 창조관 3층 301호에 있습니다.",
    )
    d = deps(
        store,
        drafted=drafted,
        finals=[
            ans("창조관 3층 301호입니다.", cited=["place:rpt-1"]),
            ans("잘 모르겠습니다.", cited=["place:rpt-1"]),
        ],
        placed=[],
        page=EV["text"],
    )
    await agent_fix.analyze(d, rid, ACTOR)
    out = await agent_fix.apply(d, rid, ACTOR)
    assert (
        out["status"] == "recheck_failed" and store.rows[rid]["status"] == "pending"
    )  # 1/2면 종결 안 함


@pytest.mark.asyncio
async def test_stale_source_needs_finished_recollect_before_verify():
    store = MemoryReportStore()
    card = {"id": "guide:web-bus:1", "title": "통학버스", "url": SRC}
    rid = report(
        store,
        "통학버스 노선 알려 줘",
        kind="stale_source",
        cards=[card],
        quotes={card["id"]: "1호차 마산역 07:40 출발 창원역 경유"},
    )
    runs = {}
    d = deps(
        store,
        page="2호차 진해 07:30 출발 (노선 변경)",
        runs=runs,
        finals=[
            ans("노선은 ...", cited=["guide:web-bus:2"]),
            ans("노선은 ...", cited=["guide:web-bus:3"]),
        ],
    )
    fix = await agent_fix.analyze(d, rid, ACTOR)
    assert (
        fix["status"] == "proposed"
        and fix["source_ids"] == ["web_pages"]
        and fix["pages"][0]["status"] == "changed"
    )
    await agent_fix.apply(d, rid, ACTOR, run_id="run-1")
    with pytest.raises(AppError):  # 재수집이 안 끝났으면 판정하지 않는다(변경 증거)
        await agent_fix.verify(d, rid, ACTOR)
    runs["run-1"] = {"status": "success"}
    out = await agent_fix.verify(d, rid, ACTOR)
    assert out["status"] == "verified" and store.rows[rid]["status"] == "resolved"


@pytest.mark.asyncio
async def test_offtopic_answer_links_unanswered_and_old_draft_expires():
    store = MemoryReportStore()
    rid = report(store, "통학버스는 아무나 탈 수 있나요?")
    d = deps(store)
    fix = await agent_fix.analyze(d, rid, ACTOR)
    assert fix["status"] == "linked" and "통학버스는 아무나 탈 수 있나요?" in store.unanswered
    store.rows[rid]["agent_fix"] = {
        "type": "place",
        "status": "proposed",
        "draft": {"drafted_at": datetime.now(UTC) - timedelta(hours=25)},
    }
    with pytest.raises(AppError):
        await agent_fix.apply(d, rid, ACTOR)


def test_not_found_answer_is_not_a_pass_and_no_source_links_unanswered():
    """실운영 테스트에서 찾은 결함: '찾지 못했습니다' 답을 통과로 보면 안 된다."""
    ok, why = resolve.run_ok(
        ans("관련 공지나 규정에서 통학버스 이용 자격 제한에 대한 내용을 찾지 못했습니다."), None
    )
    assert not ok and "찾지 못함" in why
    assert resolve.run_ok(ans("통학버스는 별도 신청 없이 캐시비카드로 이용합니다."), None)[0]


@pytest.mark.asyncio
async def test_stale_with_only_place_citation_links_unanswered():
    store = MemoryReportStore()
    card = {"id": "place:f-neoreun-madang", "title": "너른마당", "url": ""}
    rid = report(store, "통학버스 노선 알려 줘", kind="stale_source", cards=[card])
    fix = await agent_fix.analyze(deps(store), rid, ACTOR)
    assert fix["type"] == "unanswered" and fix["status"] == "linked" and fix["note"] == "no_source"


def test_same_answer_as_reported_is_not_a_fix():
    """실운영 테스트: 제보된 엉뚱한 답(교통안전관리 규정)을 다시 내면 통과가 아니다."""
    old = "교통안전관리 규정에 따르면, 이 대학교를 출입하는 모든 교통수단의 이용자들에게 해당 규정이 적용됩니다."
    ok, why = resolve.run_ok(ans(old), None, reported=old)
    assert not ok and why == "제보된 답과 같음"
    assert resolve.run_ok(
        ans("통학버스는 재학생 누구나 캐시비카드로 이용합니다."), None, reported=old
    )[0]


@pytest.mark.asyncio
async def test_unrelated_citation_does_not_close_place_report():
    """GPT5 #799-1: 위치 문자열이 있어도 반영한 장소(place:rpt-1)를 인용하지 않으면 실패."""
    store = MemoryReportStore()
    rid = report(store, "반도체부트캠프사업단이 어디있나요?")
    drafted = resolve.PlaceDraftOut(
        found=True,
        name="반도체부트캠프사업단",
        location="창조관 3층 301호",
        evidence_id=EV["id"],
        quote="반도체부트캠프사업단은 창조관 3층 301호에 있습니다.",
    )
    fake = ans("반도체부트캠프사업단은 창조관 3층 301호에 있습니다.", cited=["x"])
    d = deps(store, drafted=drafted, finals=[fake, fake], placed=[], page=EV["text"])
    await agent_fix.analyze(d, rid, ACTOR)
    out = await agent_fix.apply(d, rid, ACTOR)
    assert (
        out["status"] == "recheck_failed"
        and out["check"]["runs"][0]["reason"] == "바꾼 자료를 인용하지 않음"
    )
    assert store.rows[rid]["status"] == "pending"


@pytest.mark.asyncio
async def test_source_changed_since_draft_expires_it():
    """GPT5 #799-2: 승인 시 출처를 다시 읽어 인용문이 사라졌으면 반영하지 않는다."""
    store = MemoryReportStore()
    rid = report(store, "반도체부트캠프사업단이 어디있나요?")
    drafted = resolve.PlaceDraftOut(
        found=True,
        name="반도체부트캠프사업단",
        location="창조관 3층 301호",
        evidence_id=EV["id"],
        quote="반도체부트캠프사업단은 창조관 3층 301호에 있습니다.",
    )
    placed = []
    d = deps(store, drafted=drafted, placed=placed, page="사업단이 한마관으로 이전했습니다.")
    await agent_fix.analyze(d, rid, ACTOR)
    with pytest.raises(AppError):
        await agent_fix.apply(d, rid, ACTOR)
    assert not placed and store.rows[rid]["agent_fix"]["status"] == "stale_draft"


def test_doc_prefix():
    assert resolve.doc_prefix("guide:web-abc:3") == "guide:web-abc:"
    assert resolve.doc_prefix("196_main_30") == "196_"
    assert resolve.doc_prefix("place:rpt-1") == "place:rpt-1"

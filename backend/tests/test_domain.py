from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from backend.domain import (
    SCHEMA_VERSION,
    Answer,
    AppError,
    Correction,
    DraftSentence,
    Evidence,
    EvidenceCard,
    Profile,
    ThreadDoc,
    ToolResult,
)
from backend.domain.answer import PublicSentence


def ev(**kw):
    base = dict(
        id="101_main_32",
        kind="article",
        title="학칙 제32조(휴학)",
        text="휴학은 통산 3년을 초과할 수 없다." * 10,
    )
    base.update(kw)
    return Evidence(**base)


def test_schema_version():
    assert SCHEMA_VERSION == 1


@pytest.mark.parametrize(
    "kind", ["article", "guide", "notice", "calendar", "menu", "department", "place"]
)
def test_evidence_kinds_accepted(kind):
    assert ev(kind=kind).kind == kind


def test_evidence_rejects_unknown_kind():
    with pytest.raises(ValidationError):
        ev(kind="location")


def test_toolresult_helpers():
    assert ToolResult.fail("UPSTREAM_TIMEOUT").model_dump()["ok"] is False
    empty = ToolResult.empty()
    assert empty.ok and empty.items == [] and empty.message
    with pytest.raises(ValidationError):
        ToolResult(ok=False, error_code="WHATEVER")


def test_evidence_card_snippet_and_meta():
    as_of = datetime(2026, 9, 29, tzinfo=UTC)
    e = ev(meta={"department": "학사지원팀", "has_table": True, "as_of": as_of, "stale": True})
    card = EvidenceCard.from_evidence(e)
    assert card.state == "candidate"
    assert len(card.snippet) == 120
    assert card.department == "학사지원팀" and card.has_table and card.stale
    assert card.as_of == as_of.isoformat()


def test_profile_merge_keeps_existing_values():
    p = Profile(grade=2, scholarship="yes").merged(Profile(dept="컴퓨터공학과"))
    assert (p.grade, p.scholarship, p.dept) == (2, "yes", "컴퓨터공학과")
    assert p.filled() == {"grade", "scholarship", "dept"}


def test_profile_grade_range():
    with pytest.raises(ValidationError):
        Profile(grade=7)


def test_correction_alias_roundtrip():
    c = Correction.model_validate(
        {"from": "휴악", "to": "휴학", "confidence": 0.95, "kind": "spelling"}
    )
    assert c.from_ == "휴악"
    assert c.model_dump(by_alias=True)["from"] == "휴악"


def test_answer_does_not_expose_supporting_quotes():
    s = DraftSentence(
        text="휴학은 통산 3년을 넘을 수 없습니다.",
        cite_ids=["101_main_32"],
        supporting_quotes=["통산 3년을 초과할 수 없다"],
    )
    a = Answer(sentences=[PublicSentence.from_draft(s)], cited=["101_main_32"])
    assert "supporting_quotes" not in a.model_dump_json()
    assert a.notice


def test_thread_doc_defaults():
    t = ThreadDoc()
    assert t.clarification_count == 0 and t.pending_question is None and t.profile == Profile()


def test_app_error_status():
    e = AppError("THREAD_BUSY", "처리 중입니다")
    assert e.status == 409 and e.to_model().code == "THREAD_BUSY"

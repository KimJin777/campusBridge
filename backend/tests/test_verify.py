from backend.agent.verify import check_sentence, date_variants, normalize, verify
from backend.domain import Draft, DraftSentence, Evidence, Profile, Resolution

ART = Evidence(
    id="101_main_32",
    kind="article",
    title="학칙 제32조(휴학)",
    text=(
        "제32조(휴학) ① 휴학은 통산 3년을 초과할 수 없다. 다만, 군 입대 휴학은 포함하지 아니한다. "
        "휴학은 학기 개시일로부터 30일 이내에 신청하여야 한다."
    ),
)
GUIDE = Evidence(
    id="guide:4390:1",
    kind="guide",
    title="휴학 안내 — 신청 방법",
    text="휴학 신청은 학생포털에서 할 수 있다. 신청 후 학과 확인을 거쳐 학사지원팀이 승인한다.",
)
EV = {e.id: e for e in (ART, GUIDE)}
P = Profile()


def s(text, ids, quotes):
    return DraftSentence(text=text, cite_ids=ids, supporting_quotes=quotes)


def test_normalize_strips_space_punct_and_nfkc():
    assert normalize("휴학은  통산, 3년을!") == "휴학은통산3년을"
    assert normalize("ＡＢ") == "ab"


def test_pass():
    assert (
        check_sentence(
            s(
                "휴학은 통산 3년을 넘을 수 없습니다.",
                ["101_main_32"],
                ["휴학은 통산 3년을 초과할 수 없다"],
            ),
            EV,
            P,
        )
        is None
    )


def test_reasons():
    assert check_sentence(s("x", [], []), EV, P) == "no_cite"
    assert (
        check_sentence(s("x", ["101_main_32", "guide:4390:1"], ["휴학은 통산 3년을"]), EV, P)
        == "len_mismatch"
    )
    assert check_sentence(s("x", ["nope"], ["휴학은 통산 3년을"]), EV, P) == "unknown_id"
    assert check_sentence(s("x", ["101_main_32"], ["휴학"]), EV, P) == "quote_too_short"
    assert (
        check_sentence(s("x", ["101_main_32"], ["휴학은 통산 5년을 초과"]), EV, P)
        == "quote_not_found"
    )


def test_number_with_unit_must_be_in_cited_text():
    assert (
        check_sentence(
            s("통산 4년까지 가능합니다.", ["101_main_32"], ["휴학은 통산 3년을 초과"]), EV, P
        )
        == "number_mismatch"
    )
    assert (
        check_sentence(
            s(
                "개시일로부터 30일 이내에 신청합니다.",
                ["101_main_32"],
                ["30일 이내에 신청하여야 한다"],
            ),
            EV,
            P,
        )
        is None
    )


def test_citation_marks_and_unitless_numbers_ignored():
    assert (
        check_sentence(
            s(
                "제32조 제1항에 따라 신청은 1단계부터 합니다.",
                ["101_main_32"],
                ["30일 이내에 신청하여야"],
            ),
            EV,
            P,
        )
        is None
    )


def test_narrow_polarity_flip_is_suppressed():
    d = Draft(
        sentences=[
            s(
                "휴학 신청은 학생포털에서 할 수 없습니다.",
                ["guide:4390:1"],
                ["휴학 신청은 학생포털에서 할 수 있다"],
            )
        ]
    )
    out, report, flags = verify(d, [ART, GUIDE], P)
    assert out.sentences == [] and report.dropped[0].reason == "suppressed"
    assert flags[0].public_action == "suppress_sentence"


def test_obligation_to_permission_flip_is_suppressed():
    d = Draft(
        sentences=[
            s("30일 이내에 신청할 수 있습니다.", ["101_main_32"], ["30일 이내에 신청하여야 한다"])
        ]
    )
    out, _, flags = verify(d, [ART], P)
    assert out.sentences == [] and flags[0].code == "polarity_flip"


def test_other_risky_mismatch_is_internal_only():
    d = Draft(
        sentences=[
            s(
                "휴학은 통산 3년을 넘을 수 없습니다.",
                ["101_main_32"],
                ["휴학은 통산 3년을 초과할 수 없다"],
            )
        ]
    )
    out, _, flags = verify(d, [ART], P)
    assert len(out.sentences) == 1
    assert all(f.public_action == "internal_only" for f in flags)


def test_checklist_and_next_actions_verified_and_report():
    d = Draft(
        sentences=[
            s(
                "휴학은 통산 3년을 초과할 수 없습니다.",
                ["101_main_32"],
                ["휴학은 통산 3년을 초과할 수 없다"],
            )
        ],
        checklist=[
            s("학생포털에서 신청", ["guide:4390:1"], ["휴학 신청은 학생포털에서"]),
            s("서류 제출", [], []),
        ],
        next_actions=[s("학사지원팀 승인 대기", ["guide:4390:1"], ["학사지원팀이 승인한다"])],
    )
    out, report, _ = verify(d, [ART, GUIDE], P)
    assert len(out.checklist) == 1 and len(out.next_actions) == 1
    assert report.total == 1 and report.kept == 1
    assert [(x.section, x.reason) for x in report.dropped] == [("checklist", "no_cite")]


def test_adopted_not_cited_flag():
    d = Draft(
        sentences=[
            s("휴학 신청은 학생포털에서 합니다.", ["guide:4390:1"], ["휴학 신청은 학생포털에서"])
        ]
    )
    r = Resolution(adopted={"eligibility_or_limit": ["101_main_32"]})
    _, _, flags = verify(d, [ART, GUIDE], P, r)
    assert any(f.code == "adopted_not_cited" for f in flags)


def test_lexical_polarity_word_is_flagged_internal():
    ev = Evidence(id="x", kind="article", title="t", text="수업연한초과자의 휴학은 제한된다.")
    d = Draft(
        sentences=[s("수업연한초과자도 휴학이 허용됩니다.", ["x"], ["수업연한초과자의 휴학은"])]
    )
    out, _, flags = verify(d, [ev], P)
    assert len(out.sentences) == 1
    assert any(f.code == "risky_token" and f.public_action == "internal_only" for f in flags)


def test_iso_calendar_date_matches_korean_date_in_sentence():
    ev = Evidence(
        id="cal:1",
        kind="calendar",
        title="일정",
        text="2026-10-08 수업일수 1/3선(일반휴학 접수마감)",
    )
    ok = s(
        "일반휴학 접수는 10월 8일에 마감됩니다.", ["cal:1"], ["수업일수 1/3선(일반휴학 접수마감)"]
    )
    assert check_sentence(ok, {"cal:1": ev}, P) is None
    wrong = s(
        "일반휴학 접수는 10월 9일에 마감됩니다.", ["cal:1"], ["수업일수 1/3선(일반휴학 접수마감)"]
    )
    assert check_sentence(wrong, {"cal:1": ev}, P) == "number_mismatch"


def test_notice_dotted_dates_match_korean_dates():
    ev = Evidence(
        id="n:1",
        kind="notice",
        title="복학 등록",
        text="복학예정자는 2026. 9. 23.(수) 09:00부터 9. 29.(화) 16:00까지 등록금을 납부",
    )
    q = ["등록금을 납부"]
    ok = s("복학예정자는 9월 23일부터 9월 29일까지 등록금을 납부해야 합니다.", ["n:1"], q)
    assert check_sentence(ok, {"n:1": ev}, P) is None
    bad = s("복학예정자는 9월 30일까지 등록금을 납부해야 합니다.", ["n:1"], q)
    assert check_sentence(bad, {"n:1": ev}, P) == "number_mismatch"
    dec = Evidence(id="a", kind="article", title="t", text="평점평균이 1.50에 미만인 학생")
    assert "1월50일" not in normalize(date_variants(dec.text))


def test_table_row_number_gets_unit_from_header():
    """표 행 "졸업학점: 120"은 "120학점"으로 대조된다 — 다른 숫자는 여전히 탈락(#883)."""
    table = Evidence(
        id="29_app_3",
        kind="article",
        title="학칙 [별표 3] 졸업학점",
        text="[별표 3] 졸업학점\n[표] 단과대학: 공과대학 · 졸업학점: 130 · 입학정원: 80",
    )
    ev = {table.id: table}
    quote = ["단과대학: 공과대학 · 졸업학점: 130"]
    assert (
        check_sentence(s("공과대학은 130학점을 이수해야 졸업합니다.", ["29_app_3"], quote), ev, P)
        is None
    )
    assert check_sentence(s("입학정원은 80명입니다.", ["29_app_3"], quote), ev, P) is None
    assert (
        check_sentence(s("공과대학은 140학점이 필요합니다.", ["29_app_3"], quote), ev, P)
        == "number_mismatch"
    )

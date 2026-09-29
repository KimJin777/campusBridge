from backend.agent.needs import plan_calls
from backend.agent.slots import ask_text, choices_for, missing_slots, required_slots
from backend.domain import Profile
from backend.store.mask import mask


def names(calls):
    return [c["name"] for c in calls]


def test_leave_scenario_plan():
    calls = plan_calls(
        ["procedure_and_contact", "current_deadline", "eligibility_or_limit"],
        search_query="휴학 신청",
        topic="휴학",
    )
    assert names(calls) == ["search_academic_knowledge", "get_academic_calendar", "get_notices"]
    assert calls[0]["args"]["kinds"] == ["guide", "rule"]
    assert calls[2]["args"] == {"board": "academic", "keyword": "휴학"}
    assert [c["id"] for c in calls] == ["plan_0", "plan_1", "plan_2"]


def test_single_search_call_and_original_query():
    calls = plan_calls(
        ["eligibility_or_limit"], search_query="휴학 기간", original_query="휴악 기간"
    )
    assert names(calls) == ["search_academic_knowledge"]
    assert calls[0]["args"] == {
        "query": "휴학 기간",
        "kinds": ["rule"],
        "original_query": "휴악 기간",
    }


def test_menu_and_location_without_search():
    assert names(plan_calls(["menu"], search_query="학식")) == ["get_menu"]
    assert names(plan_calls(["location"], search_query="학사지원팀 위치")) == [
        "find_campus_location"
    ]
    assert plan_calls([], search_query="x") == []


def test_slots():
    assert required_slots("휴학") == []  # 휴학·수강 절차는 되묻지 않음(#613)
    assert required_slots(None, "수강 철회 언제까지") == []
    assert required_slots("졸업") == ["dept"]
    assert required_slots("식단") == []
    assert missing_slots(["grade", "scholarship"], Profile(grade=2)) == ["scholarship"]


def test_ask_text_particles_and_merge():
    assert ask_text(["grade", "scholarship"]) == "학년과 이번 학기 장학금 수혜 여부를 알려 주세요."
    assert ask_text(["grade"]) == "학년을 알려 주세요."
    assert (
        ask_text(["grade"], confirm_term="휴학")
        == "'휴학'을 말씀하신 건가요? 맞다면 학년을 알려 주세요."
    )
    assert choices_for(["grade", "dept"]) == {"grade": ["1", "2", "3", "4", "5 이상"]}


def test_mask():
    m = mask("제 이름은 홍길동이고 학번 20231234, 010-1234-5678, hong@kyungnam.ac.kr 입니다. 2학년")
    assert "홍길동" not in m and "20231234" not in m and "5678" not in m and "hong@" not in m
    assert "[학번]" in m and "[전화]" in m and "[이메일]" in m and "[이름]" in m
    assert "2학년" in m
    assert mask("2026-2학기 휴학") == "2026-2학기 휴학"


def test_notice_board_selection():
    calls = plan_calls([], search_query="최근 장학 공지", topic="장학 공지", intent="notice")
    assert calls[0]["args"] == {"board": "scholarship", "keyword": ""}
    calls = plan_calls(
        ["current_deadline"], search_query="국가장학금 신청 기한", topic="국가장학금"
    )
    assert calls[-1]["args"] == {"board": "scholarship", "keyword": "국가장학금"}
    calls = plan_calls(["current_deadline"], search_query="휴학 신청 기한", topic="휴학")
    assert calls[-1]["args"] == {"board": "academic", "keyword": "휴학"}


def test_slots_only_where_answer_depends_on_condition():
    assert required_slots("휴학", intent="procedure") == []  # 20문항 #13·14
    assert required_slots("수강철회", intent="procedure") == []
    assert required_slots("졸업", intent="procedure") == ["dept"]  # 학과별 요건이 다름
    assert required_slots("졸업", "졸업 학점 몇 점", intent="rule") == []


def test_generic_notice_topic_means_latest_notices():
    from backend.agent.needs import notice_keyword

    assert notice_keyword("이번 주 주요 학교 공지사항") == ""
    assert notice_keyword("근로장학생 모집 공지") == "근로장학생 모집"


def test_target_day_and_semester_window():
    from datetime import date

    from backend.agent.needs import semester_window, target_day

    today = date(2026, 9, 29)
    assert target_day("내일 학생식당 메뉴", today) == date(2026, 9, 30)
    assert target_day("모레 메뉴", today) == date(2026, 10, 1)
    assert target_day("학식 메뉴", today) == today
    assert semester_window(today) == (date(2026, 9, 1), date(2027, 2, 28))
    assert semester_window(date(2027, 1, 10)) == (date(2026, 9, 1), date(2027, 2, 28))
    assert semester_window(date(2026, 4, 1)) == (date(2026, 3, 1), date(2026, 8, 31))

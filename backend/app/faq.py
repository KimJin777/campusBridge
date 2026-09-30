"""첫 화면 '자주 묻는 질문' — 시기(월)별로 나눠 제시(교수님 #768·#769).

수강신청 무렵엔 수강신청, 학기 중엔 식단·통학버스, 방학 직전엔 계절학기처럼
그달에 많이 묻는 분류를 먼저 보여 주고, '전체 보기'에서 모든 분류를 보여 준다.
모두 우리 서비스가 원문 근거로 답할 수 있는 학사·생활 질문만 넣는다(범위 밖 예시 금지).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

KST = timezone(timedelta(hours=9), "KST")
MAX_CHIPS = 6
ALWAYS = "통학버스(스쿨버스) 타는 곳이 어디예요?"  # 교수님 #768: 자주 묻는 질문에 통학버스

# (분류, 많이 묻는 달, 질문) — 달은 학사 일정 기준 대략값
CATALOG: tuple[tuple[str, tuple[int, ...], tuple[str, ...]], ...] = (
    (
        "수강신청",
        (1, 2, 3, 7, 8, 9),
        (
            "수강신청 기간 언제예요?",
            "수강 정정 기간 알려 주세요",
            "한 학기 최대 몇 학점까지 들을 수 있어요?",
        ),
    ),
    (
        "휴학·복학",
        (1, 2, 3, 7, 8, 9),
        ("휴학 신청 절차 알려 주세요", "복학 신청 언제까지예요?", "군휴학 신청 방법"),
    ),
    (
        "장학금",
        (1, 2, 3, 7, 8, 9),
        ("장학금 신청 언제까지예요?", "국가장학금과 교내장학금 중복으로 받을 수 있어요?"),
    ),
    (
        "학교생활",
        (3, 4, 5, 6, 9, 10, 11, 12),
        ("오늘 학생식당 메뉴", ALWAYS, "학사관리팀 어디 있어요?"),
    ),
    (
        "시험·성적",
        (4, 6, 10, 12, 1, 7),
        ("중간고사 기간 언제예요?", "성적 경고 기준과 재수강 규정", "성적 이의신청 방법"),
    ),
    (
        "계절학기",
        (5, 6, 11, 12),
        ("계절학기 신청 기간 언제예요?", "계절학기 최대 몇 학점까지 들을 수 있어요?"),
    ),
    (
        "졸업",
        (4, 5, 10, 11, 12),
        ("졸업 요건 알려 주세요", "조기졸업 조건이 뭐예요?"),
    ),
)


def monthly(today: date) -> dict[str, Any]:
    """이달 추천 칩(최대 6개, 통학버스 항상 포함)과 전체 분류."""
    m = today.month
    hot = [(label, qs) for label, months, qs in CATALOG if m in months]
    chips: list[str] = []
    for i in range(3):  # 분류마다 돌아가며 한 개씩 → 한 분류가 칩을 독차지하지 않게
        for _, qs in hot:
            if i < len(qs) and qs[i] not in chips:
                chips.append(qs[i])
    chips = chips[: MAX_CHIPS - 1] if ALWAYS not in chips[:MAX_CHIPS] else chips[:MAX_CHIPS]
    if ALWAYS not in chips:
        chips.append(ALWAYS)
    groups = [
        {"label": label, "hot": m in months, "items": list(qs)} for label, months, qs in CATALOG
    ]
    groups.sort(key=lambda g: not g["hot"])  # 이달 분류를 위로
    return {"month": m, "items": chips, "groups": groups}


def this_month() -> dict[str, Any]:
    return monthly(datetime.now(KST).date())

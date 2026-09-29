"""의도·주제별 필요 조건(slot) 정의(상세설계 02 §4-2 표)와 되묻기 질문 생성."""

from __future__ import annotations

from backend.domain.thread import Profile

# 주제 키워드 → 필요 slot. 첫 일치만 쓴다(순서 중요).
# 2026-09-29 교수님 실측(#607·#613 합의): 답이 조건에 따라 **실제로 달라지는** 경우에만 되묻는다.
# 휴학·복학·수강 변경 절차는 학년·장학과 무관하게 같은 절차 → 되묻지 않고 일반 답변(조건별 차이는
# 답변에서 안내). 졸업 절차만 학과별 요건이 달라 소속 학과를 묻는다.
TOPIC_SLOTS: list[tuple[tuple[str, ...], list[str]]] = [
    (("졸업",), ["dept"]),
]

SLOT_QUESTION = {
    "grade": "학년",
    "scholarship": "이번 학기 장학금 수혜 여부",
    "dept": "소속 학과",
}

SLOT_CHOICES: dict[str, list[str]] = {
    "grade": ["1", "2", "3", "4", "5 이상"],
    "scholarship": ["예", "아니오", "모름"],
}


# 조건을 되물을 의도 — 02 §4-2 표의 "휴학·복학 절차", "수강 신청·변경·철회"는 절차 질문일 때만.
# 규정 해석 질문("휴학 최대 몇 학기?")에 학년·장학을 묻는 것은 불필요한 마찰(평가 표본 실측).
PROCEDURE_ONLY = {("휴학", "복학")}


def required_slots(topic: str | None, query: str = "", intent: str | None = None) -> list[str]:
    hay = f"{topic or ''} {query}"
    for keys, slots in TOPIC_SLOTS:
        if any(k in hay for k in keys):
            if intent not in (None, "procedure"):
                return []  # 교수님 질문리스트 실측: 규정·일정 질문에 학년을 되묻는 마찰 제거
            return list(slots)
    return []


def missing_slots(needed: list[str], profile: Profile) -> list[str]:
    filled = profile.filled()
    return [s for s in needed if s not in filled]


def _has_batchim(word: str) -> bool:
    c = word[-1]
    return "가" <= c <= "힣" and (ord(c) - 0xAC00) % 28 != 0


def _obj(word: str) -> str:
    return word + ("을" if _has_batchim(word) else "를")


def _and(word: str) -> str:
    return word + ("과" if _has_batchim(word) else "와")


def ask_text(missing: list[str], confirm_term: str | None = None) -> str:
    """되묻기 문장. 오타 확인과 조건 되묻기는 한 질문으로 합친다(02 §4-2-1)."""
    parts = [SLOT_QUESTION[s] for s in missing if s in SLOT_QUESTION]
    cond = ""
    if parts:
        joined = (
            parts[0]
            if len(parts) == 1
            else ", ".join(parts[:-2] + [_and(parts[-2])]) + " " + parts[-1]
        )
        cond = f"{_obj(joined)} 알려 주세요."
    if confirm_term:
        lead = f"'{confirm_term}'{_obj(confirm_term)[len(confirm_term) :]} 말씀하신 건가요?"
        return f"{lead} 맞다면 {cond}" if cond else lead
    return cond


def choices_for(missing: list[str]) -> dict[str, list[str]]:
    return {s: SLOT_CHOICES[s] for s in missing if s in SLOT_CHOICES}

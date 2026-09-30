"""답변 뒤 '이어서 물어보기' 예상 질문(교수님 #638·#641·#644).

작성 모델(compose)이 근거와 대화 흐름을 보고 2~3개를 함께 내고, 서버가 거른다:
이미 물은 질문·너무 긴 질문·개인정보를 요구하는 질문을 빼고, 비면 주제별 기본 질문으로 채운다.
"""

from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

MAX_FOLLOW_UPS = 3
MAX_LEN = 40
RECENT_KEEP = 3
# 기본 질문 회귀 점검 결과(eval/follow_up_check.py) — 답을 못 한 질문은 칩으로 내지 않는다
COVERAGE_PATH = Path(__file__).resolve().parents[1] / "data" / "follow_up_coverage.json"

# 개인정보를 요구하거나 서비스 범위 밖으로 이끄는 표현
BLOCK = re.compile(r"학번|주민|전화번호|연락처를 알려|비밀번호|계좌|주식|코인|날씨|연애")

TOPIC_DEFAULTS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (
        ("휴학",),
        (
            "복학 신청은 언제 하나요?",
            "휴학하면 등록금은 어떻게 되나요?",
            "군휴학 서류는 뭐가 필요해요?",
        ),
    ),
    (
        ("복학",),
        (
            "복학 후 수강신청은 언제 하나요?",
            "등록금 납부 기간은 언제인가요?",
            "휴학 연장은 어떻게 하나요?",
        ),
    ),
    (
        ("장학",),
        (
            "성적우수장학금 기준이 뭐예요?",
            "국가장학금이랑 같이 받을 수 있나요?",
            "장학금 신청 기간은 언제예요?",
        ),
    ),
    (
        ("수강", "재수강"),
        ("수강 정정 기간은 언제예요?", "재수강 기준이 어떻게 돼요?", "학기당 최대 신청 학점은요?"),
    ),
    (
        ("졸업",),
        ("조기졸업 기준은 뭐예요?", "졸업 학점은 몇 점이에요?", "졸업 연기는 어떻게 하나요?"),
    ),
    (
        ("식단", "메뉴", "학식"),
        ("내일 학생식당 메뉴 알려줘", "푸드코트 메뉴는 뭐예요?", "지난주 월요일 식단은요?"),
    ),
    (
        ("기숙사", "생활관"),
        (
            "기숙사 중도 퇴사는 어떻게 해요?",
            "생활관 입사 자격이 뭐예요?",
            "생활관비는 어떻게 내나요?",
        ),
    ),
)


@lru_cache(maxsize=1)
def failed_defaults() -> frozenset[str]:
    try:
        return frozenset(json.loads(COVERAGE_PATH.read_text(encoding="utf-8")).get("failed", []))
    except (OSError, ValueError):
        return frozenset()


def _norm(text: str) -> str:
    return re.sub(r"[\s?.!,~·]+", "", unicodedata.normalize("NFKC", text)).casefold()


def pick_follow_ups(
    proposed: list[str], *, asked: list[str], topic: str | None = None, query: str = ""
) -> list[str]:
    """모델 제안 → 거르기 → 부족하면 주제별 기본 질문으로 채움. 최대 3개."""
    seen = {_norm(q) for q in asked if q}
    out: list[str] = []

    def add(q: str) -> None:
        q = " ".join(str(q).split())
        key = _norm(q)
        if not q or len(q) > MAX_LEN or key in seen or BLOCK.search(q):
            return
        seen.add(key)
        out.append(q)

    for q in proposed:
        if len(out) >= MAX_FOLLOW_UPS:
            break
        add(q)
    if len(out) < 2:
        hay = f"{topic or ''} {query}"
        for keys, defaults in TOPIC_DEFAULTS:
            if any(k in hay for k in keys):
                for q in defaults:
                    if len(out) >= MAX_FOLLOW_UPS:
                        break
                    if q not in failed_defaults():
                        add(q)
                break
    return out


def next_recent(prev_recent: list[str], prev_query: str | None) -> list[str]:
    """스레드에 남길 최근 질문(오래된 것부터, 최대 3개)."""
    items = [*prev_recent, *([prev_query] if prev_query else [])]
    return items[-RECENT_KEEP:]

"""개인정보 마스킹(상세설계 02 §5) — API 계층이 요청을 받자마자 적용한다.

모든 Gemini 호출과 저장에는 mask() 결과(query_for_model)만 쓴다. 원문은 메모리에만 둔다.
"""

from __future__ import annotations

import re

_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[이메일]"),
    (re.compile(r"(?<!\d)01\d-?\d{3,4}-?\d{4}(?!\d)"), "[전화]"),
    (re.compile(r"(?<!\d)\d{8,10}(?!\d)"), "[학번]"),
    # 이름은 오탐이 많아 "제 이름은 ○○○" 형태만 보수적으로
    (re.compile(r"(제\s*이름은\s*)[가-힣]{2,4}"), r"\1[이름]"),
]


def mask(text: str) -> str:
    for pat, rep in _RULES:
        text = pat.sub(rep, text)
    return text

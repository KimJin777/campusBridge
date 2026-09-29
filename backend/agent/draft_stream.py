"""compose 스트리밍 중 부분 JSON에서 답변 문장만 뽑는다(교수님 승인 — 체감 속도 개선).

모델은 ComposeOut JSON을 그대로 스트리밍하고, 여기서는 `"text": "..."` 값만 꺼내
화면에 초안으로 보낸다. 초안은 검증 전이므로 화면은 "검증 중"으로만 표시하고,
확정 답변은 verify 통과 후 answer 이벤트로 교체한다.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

TEXT_DONE = re.compile(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)"')
TEXT_OPEN = re.compile(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)\\?$')
MIN_GROWTH = 12  # 이만큼 글자가 늘 때마다 보낸다(이벤트 폭주 방지)


def _unescape(raw: str) -> str | None:
    try:
        return json.loads(f'"{raw}"')
    except ValueError:  # \u00 처럼 이스케이프가 잘린 조각
        return None


def partial_texts(buf: str) -> list[str]:
    """완성된 text 값 + 작성 중인 마지막 text 값(있으면)."""
    out: list[str] = []
    end = 0
    for m in TEXT_DONE.finditer(buf):
        text = _unescape(m.group(1))
        if text is not None:
            out.append(text)
        end = m.end()
    m = TEXT_OPEN.search(buf, end)
    if m:
        text = _unescape(m.group(1))
        if text:
            out.append(text)
    return out


def draft_emitter(emit: Callable[[list[str]], None]) -> Callable[[str], None]:
    """누적 버퍼를 받아 바뀐 초안만 emit한다. 빈 버퍼 = 새 시도(재시도·대체 모델) → 초안 초기화."""
    last: list[str] = []

    def on_text(buf: str) -> None:
        nonlocal last
        if not buf:
            if last:
                last = []
                emit([])
            return
        texts = partial_texts(buf)
        grown = sum(map(len, texts)) - sum(map(len, last))
        if texts and (len(texts) != len(last) or grown >= MIN_GROWTH):
            last = texts
            emit(texts)

    return on_text

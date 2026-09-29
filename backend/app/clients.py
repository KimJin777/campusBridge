"""공통 호출 래퍼(상세설계 07 §5) — Gemini·Vertex AI Search·학교 사이트 HTTP 모두 이 함수를 거친다.

- 호출마다 min(timeout, deadline - now)로 제한
- 지연 = base * 2**n * (0.5 + random()); 다음 시도가 deadline을 넘기면 재시도하지 않고 실패
- 재시도 대상(429·5xx·타임아웃) 외 예외는 즉시 전파
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable

log = logging.getLogger("campusbridge.clients")

RETRY_STATUS = (429, 500, 502, 503, 504)

# 07 §5 호출별 개별 제한(초)·최대 재시도 횟수. attempts = 재시도 + 1
PROFILES: dict[str, tuple[float, int]] = {
    "search": (5.0, 2),
    "school_http": (5.0, 1),
    "gemini_classify": (8.0, 2),
    "gemini_compose": (15.0, 1),
    "batch": (30.0, 5),  # 평가 실행기·수집 Job(대화 마감 없음)
}


class CallFailed(Exception):
    """재시도 후에도 실패. 도구는 이것을 ToolResult(ok=False)로 바꾼다."""

    def __init__(self, target: str, attempts: int, cause: BaseException, timed_out: bool):
        super().__init__(f"{target} failed after {attempts} attempt(s): {cause!r}")
        self.target = target
        self.attempts = attempts
        self.cause = cause
        self.timed_out = timed_out


def status_of(exc: BaseException) -> int | None:
    """SDK마다 다른 HTTP 상태 표기를 하나로 읽는다(httpx·google-genai·google-api-core)."""
    for attr in ("status_code", "code"):
        v = getattr(exc, attr, None)
        if isinstance(v, int):
            return v
    resp = getattr(exc, "response", None)
    v = getattr(resp, "status_code", None)
    return v if isinstance(v, int) else None


def _retryable(exc: BaseException, retry_on: tuple[int, ...]) -> bool:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return True
    return status_of(exc) in retry_on


def deadline_after(ms: int) -> float:
    """턴 마감 시각(monotonic 초)."""
    return time.monotonic() + ms / 1000


def remaining(deadline: float | None) -> float | None:
    return None if deadline is None else deadline - time.monotonic()


async def call_with_retry[T](
    fn: Callable[[], Awaitable[T]],
    *,
    timeout: float,  # noqa: ASYNC109 — 07 §5 시그니처(호출당 강제 중단값)
    deadline: float | None = None,
    attempts: int = 3,
    base: float = 1.0,
    retry_on: tuple[int, ...] = RETRY_STATUS,
    target: str = "call",
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rand: Callable[[], float] = random.random,
) -> T:
    started = time.monotonic()
    last: BaseException | None = None
    timed_out = False
    made = 0
    for n in range(attempts):
        left = remaining(deadline)
        if left is not None and left <= 0:
            timed_out = True
            break
        limit = timeout if left is None else min(timeout, left)
        made += 1
        try:
            result = await asyncio.wait_for(fn(), timeout=limit)
            _log(target, started, made - 1, "ok")
            return result
        except Exception as exc:  # noqa: BLE001 — 분류 후 재전파
            if not _retryable(exc, retry_on):
                _log(target, started, made - 1, f"error:{type(exc).__name__}")
                raise
            last = exc
            timed_out = isinstance(exc, (asyncio.TimeoutError, TimeoutError))
        if n + 1 >= attempts:
            break
        delay = base * (2**n) * (0.5 + rand())
        left = remaining(deadline)
        if left is not None and delay >= left:
            break  # 다음 시도가 마감을 넘긴다 → 재시도하지 않음
        await sleep(delay)
    cause = last or TimeoutError("deadline exceeded before call")
    _log(
        target, started, max(made - 1, 0), "timeout" if timed_out else f"status:{status_of(cause)}"
    )
    raise CallFailed(target, made, cause, timed_out)


async def call_profile[T](
    profile: str,
    fn: Callable[[], Awaitable[T]],
    *,
    deadline: float | None = None,
    target: str | None = None,
) -> T:
    """07 §5 표의 호출 종류 이름으로 부른다."""
    timeout, retries = PROFILES[profile]
    return await call_with_retry(
        fn, timeout=timeout, deadline=deadline, attempts=retries + 1, target=target or profile
    )


def _log(target: str, started: float, retries: int, result: str) -> None:
    log.info(
        json.dumps(
            {
                "event": "external_call",
                "target": target,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "retries": retries,
                "result": result,
            },
            ensure_ascii=False,
        )
    )

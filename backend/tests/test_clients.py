import asyncio

import pytest

from backend.app.clients import CallFailed, call_with_retry, deadline_after, status_of


class HttpErr(Exception):
    def __init__(self, status_code):
        super().__init__(status_code)
        self.status_code = status_code


def run(coro):
    return asyncio.run(coro)


def flaky(errors, value="ok"):
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        if errors:
            raise errors.pop(0)
        return value

    return fn, calls


async def no_sleep(_):
    return None


def test_success_first_try():
    fn, calls = flaky([])
    assert run(call_with_retry(fn, timeout=1, sleep=no_sleep)) == "ok"
    assert calls["n"] == 1


def test_retries_on_429_then_succeeds():
    fn, calls = flaky([HttpErr(429), HttpErr(503)])
    assert run(call_with_retry(fn, timeout=1, attempts=3, sleep=no_sleep)) == "ok"
    assert calls["n"] == 3


def test_non_retryable_propagates_immediately():
    fn, calls = flaky([HttpErr(400)])
    with pytest.raises(HttpErr):
        run(call_with_retry(fn, timeout=1, attempts=3, sleep=no_sleep))
    assert calls["n"] == 1


def test_exhausted_raises_callfailed_with_attempts():
    fn, calls = flaky([HttpErr(500), HttpErr(500), HttpErr(500)])
    with pytest.raises(CallFailed) as ei:
        run(call_with_retry(fn, timeout=1, attempts=3, sleep=no_sleep, target="search"))
    assert ei.value.attempts == 3 and calls["n"] == 3
    assert status_of(ei.value.cause) == 500 and not ei.value.timed_out


def test_timeout_is_retryable_and_flagged():
    async def slow():
        await asyncio.sleep(1)

    with pytest.raises(CallFailed) as ei:
        run(call_with_retry(slow, timeout=0.01, attempts=2, sleep=no_sleep))
    assert ei.value.timed_out and ei.value.attempts == 2


def test_no_retry_when_backoff_would_pass_deadline():
    fn, calls = flaky([HttpErr(429), HttpErr(429)])
    # 지연 = base(10) * (0.5 + 0.5) = 10초 > 남은 시간 → 재시도하지 않음
    with pytest.raises(CallFailed) as ei:
        run(
            call_with_retry(
                fn,
                timeout=1,
                deadline=deadline_after(2000),
                attempts=3,
                base=10,
                sleep=no_sleep,
                rand=lambda: 0.5,
            )
        )
    assert calls["n"] == 1 and ei.value.attempts == 1


def test_expired_deadline_makes_no_call():
    fn, calls = flaky([])
    with pytest.raises(CallFailed) as ei:
        run(call_with_retry(fn, timeout=1, deadline=deadline_after(-1), sleep=no_sleep))
    assert calls["n"] == 0 and ei.value.attempts == 0 and ei.value.timed_out


def test_call_limited_by_remaining_deadline():
    async def slow():
        await asyncio.sleep(1)

    with pytest.raises(CallFailed):
        run(
            call_with_retry(
                slow, timeout=10, deadline=deadline_after(50), attempts=1, sleep=no_sleep
            )
        )


def test_status_of_reads_response_attribute():
    class Resp:
        status_code = 502

    class E(Exception):
        response = Resp()

    assert status_of(E()) == 502

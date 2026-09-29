import asyncio

import pytest
from pydantic import BaseModel

from backend.agent import llm as llm_mod
from backend.agent.llm import GeminiLLM, LLMTimeout, LLMUnavailable
from backend.app.clients import deadline_after
from backend.app.config import Settings


class Out(BaseModel):
    ok: bool


class FakeChat:
    def __init__(self, behavior):
        self.behavior = behavior

    def with_structured_output(self, schema):
        return self

    async def ainvoke(self, messages):
        return await self.behavior()


def make(behaviors):
    g = GeminiLLM(Settings())
    g._chat = lambda model, thinking: FakeChat(behaviors[model])  # type: ignore[method-assign]
    return g


async def hang():
    await asyncio.sleep(10)


async def ok():
    return Out(ok=True)


class Http(Exception):
    def __init__(self, code):
        self.code = code


async def rate_limited():
    raise Http(429)


def test_primary_hang_falls_back_quickly(monkeypatch):
    monkeypatch.setitem(llm_mod.PRIMARY_TIMEOUT, "classify", 0.05)
    g = make({"gemini-3.5-flash": hang, "gemini-2.5-flash": ok})
    out = asyncio.run(g.structured(Out, "s", "u", node="classify", deadline=deadline_after(5000)))
    assert out.ok


def test_primary_429_falls_back():
    g = make({"gemini-3.5-flash": rate_limited, "gemini-2.5-flash": ok})
    out = asyncio.run(g.structured(Out, "s", "u", node="compose", deadline=deadline_after(20000)))
    assert out.ok


def test_both_time_out_raises_llm_timeout(monkeypatch):
    monkeypatch.setitem(llm_mod.PRIMARY_TIMEOUT, "compose", 0.05)
    monkeypatch.setitem(llm_mod.PROFILES, "gemini_compose", (0.05, 0))
    g = make({"gemini-3.5-flash": hang, "gemini-2.5-flash": hang})
    with pytest.raises(LLMTimeout):
        asyncio.run(g.structured(Out, "s", "u", node="compose", deadline=deadline_after(20000)))


def test_no_time_left_for_fallback_raises_unavailable(monkeypatch):
    g = make({"gemini-3.5-flash": rate_limited, "gemini-2.5-flash": ok})
    with pytest.raises(LLMUnavailable):
        asyncio.run(g.structured(Out, "s", "u", node="classify", deadline=deadline_after(1000)))

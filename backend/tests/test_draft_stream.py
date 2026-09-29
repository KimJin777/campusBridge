import asyncio
from types import SimpleNamespace

from pydantic import BaseModel

from backend.agent import llm as llm_mod
from backend.agent.draft_stream import draft_emitter, partial_texts
from backend.agent.llm import GeminiLLM
from backend.app.clients import deadline_after
from backend.app.config import Settings


def test_partial_texts_complete_and_open():
    buf = '{"sentences":[{"text":"휴학은 학기 개시 전","cite_ids":["a"]},{"text":"신청서는 \\"학사'
    assert partial_texts(buf) == ["휴학은 학기 개시 전", '신청서는 "학사']


def test_partial_texts_ignores_cut_escape_and_quotes():
    assert partial_texts('{"sentences":[{"text":"a\\u00') == []
    buf = '{"sentences":[{"text":"x","supporting_quotes":["\\"text\\": 가짜"]}]}'
    assert partial_texts(buf) == ["x"]


def test_emitter_throttles_and_resets():
    sent = []
    on = draft_emitter(sent.append)
    on('{"sentences":[{"text":"가')
    on('{"sentences":[{"text":"가나')  # 12자 미만 증가 → 생략
    on('{"sentences":[{"text":"가나다라마바사아자차카타파하","cite_ids":[')
    on("")  # 새 시도
    assert sent == [["가"], ["가나다라마바사아자차카타파하"], []]


class Out(BaseModel):
    text: str


class StreamChat:
    def __init__(self, pieces):
        self.pieces = pieces

    def bind(self, **kwargs):
        assert kwargs["response_mime_type"] == "application/json"
        return self

    async def astream(self, messages):
        for p in self.pieces:
            yield SimpleNamespace(content=p)


class HangChat(StreamChat):
    async def astream(self, messages):
        await asyncio.sleep(10)
        yield SimpleNamespace(content="")


def test_stream_structured_falls_back_and_resets(monkeypatch):
    monkeypatch.setitem(llm_mod.PRIMARY_TIMEOUT, "compose", 0.05)
    g = GeminiLLM(Settings())
    chats = {
        "gemini-3.5-flash": HangChat([]),
        "gemini-3.5-flash-lite": StreamChat(['{"text":"안', [{"type": "text", "text": '녕"}'}]]),
    }
    g._chat = lambda model, thinking: chats[model]  # type: ignore[method-assign]
    seen: list[str] = []
    out = asyncio.run(
        g.stream_structured(
            Out, "s", "u", node="compose", deadline=deadline_after(20000), on_text=seen.append
        )
    )
    assert out.text == "안녕"
    assert seen.count("") == 2  # 시도마다 초기화
    assert seen[-1] == '{"text":"안녕"}'

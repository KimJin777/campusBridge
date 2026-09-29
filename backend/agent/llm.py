"""그래프 노드가 쓰는 구조화 출력 LLM 경계.

노드는 StructuredLLM 프로토콜에만 의존한다.
테스트는 가짜 구현, 운영은 GeminiLLM(LangChain ChatVertexAI).
모든 호출은 공통 래퍼(07 §5)를 거치고, 재시도 소진 시 대체 모델로 한 번 더 시도한다.
"""

from __future__ import annotations

from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from backend.app.clients import CallFailed, call_profile, remaining
from backend.app.config import Settings, get_settings

T = TypeVar("T", bound=BaseModel)


class StructuredOutputError(Exception):
    """모델 응답이 스키마에 맞지 않음 — 노드가 1회 재시도 후 fallback/error로 처리한다."""


class LLMUnavailable(Exception):
    """기본·대체 모델 모두 호출 실패."""


class StructuredLLM(Protocol):
    async def structured(
        self, schema: type[T], system: str, user: str, *, node: str, deadline: float | None
    ) -> T: ...


PROFILE_BY_NODE = {
    "classify": "gemini_classify",
    "act": "gemini_classify",
    "compose": "gemini_compose",
}
MIN_FALLBACK_SECONDS = 3.0


class GeminiLLM:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()
        self._models: dict[str, object] = {}

    def _chat(self, model: str, thinking: str | None):
        """노드별 thinking 수준을 달리한 채팅 모델(지연 생성·캐시).

        ChatVertexAI는 LangChain 3.2에서 폐기 예정 → ChatGoogleGenerativeAI(vertexai=True) 우선.
        thinking_level은 3.x 모델에만 넘긴다(2.5는 미지원).
        """
        key = f"{model}|{thinking}"
        if key not in self._models:
            common = {
                "model": model,
                "project": self.s.gcp_project_id or None,
                "location": self.s.gemini_location,
                "temperature": 0,
                "max_output_tokens": self.s.max_output_tokens,
                "max_retries": 0,  # 재시도는 공통 래퍼가 담당
            }
            use_thinking = thinking and model.startswith("gemini-3")
            try:
                from langchain_google_genai import ChatGoogleGenerativeAI

                extra = {"thinking_level": thinking} if use_thinking else {}
                self._models[key] = ChatGoogleGenerativeAI(vertexai=True, **common, **extra)
            except ImportError:
                from langchain_google_vertexai import ChatVertexAI

                self._models[key] = ChatVertexAI(**common)
        return self._models[key]

    async def _once(
        self, model: str, schema: type[T], system: str, user: str, node: str, deadline: float | None
    ) -> T:
        thinking = self.s.classify_thinking if node in ("classify", "act") else None
        runnable = self._chat(model, thinking).with_structured_output(schema)

        async def call():
            return await runnable.ainvoke([("system", system), ("human", user)])

        try:
            out = await call_profile(
                PROFILE_BY_NODE.get(node, "gemini_classify"),
                call,
                deadline=deadline,
                target=f"gemini:{node}",
            )
        except (ValidationError, ValueError) as e:
            raise StructuredOutputError(str(e)) from e
        if out is None:
            raise StructuredOutputError("empty structured output")
        return out if isinstance(out, schema) else schema.model_validate(out)

    async def structured(
        self, schema: type[T], system: str, user: str, *, node: str, deadline: float | None
    ) -> T:
        try:
            return await self._once(self.s.gemini_model, schema, system, user, node, deadline)
        except CallFailed as first:
            left = remaining(deadline)
            if not self.s.gemini_fallback_model or (
                left is not None and left < MIN_FALLBACK_SECONDS
            ):
                raise LLMUnavailable(str(first)) from first
            try:
                return await self._once(
                    self.s.gemini_fallback_model, schema, system, user, node, deadline
                )
            except CallFailed as second:
                raise LLMUnavailable(str(second)) from second

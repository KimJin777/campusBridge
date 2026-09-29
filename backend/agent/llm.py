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

    def _chat(self, model: str):
        if model not in self._models:
            from langchain_google_vertexai import ChatVertexAI  # 지연 import — 테스트에 SDK 불필요

            self._models[model] = ChatVertexAI(
                model=model,
                project=self.s.gcp_project_id or None,
                location=self.s.gemini_location,
                temperature=0,
                max_output_tokens=self.s.max_output_tokens,
                max_retries=0,  # 재시도는 공통 래퍼가 담당
            )
        return self._models[model]

    async def _once(
        self, model: str, schema: type[T], system: str, user: str, node: str, deadline: float | None
    ) -> T:
        runnable = self._chat(model).with_structured_output(schema)

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

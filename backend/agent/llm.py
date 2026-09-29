"""그래프 노드가 쓰는 구조화 출력 LLM 경계.

노드는 StructuredLLM 프로토콜에만 의존한다.
테스트는 가짜 구현, 운영은 GeminiLLM(LangChain ChatGoogleGenerativeAI, vertexai=True).

호출 전략(2026-09-29 실측 → hedging):
- 기본 모델(gemini-3.5-flash)은 중앙값 2초지만 가끔 30초 이상 멈춘다(classify 1/8, compose 2/6).
- 대체 모델(gemini-2.5-flash, thinking 0)은 최대 2.5초로 안정적이다.
→ 기본 모델을 **짧은 제한시간·재시도 없이** 1회 시도하고, 시간 초과·429·5xx면 곧바로 대체 모델로
  공통 래퍼(07 §5)의 정상 제한시간·재시도로 호출한다. 모델 교체가 아니라 꼬리 지연 차단이다.
"""

from __future__ import annotations

from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from backend.app.clients import PROFILES, CallFailed, call_with_retry, remaining
from backend.app.config import Settings, get_settings

T = TypeVar("T", bound=BaseModel)


class StructuredOutputError(Exception):
    """모델 응답이 스키마에 맞지 않음 — 노드가 1회 재시도 후 fallback/error로 처리한다."""


class LLMUnavailable(Exception):
    """기본·대체 모델 모두 호출 실패."""


class LLMTimeout(LLMUnavailable):
    """시간 초과로 실패 — 턴 마감 문제이므로 compose는 fallback(deadline)으로 끝낸다(02 §7)."""


class StructuredLLM(Protocol):
    async def structured(
        self, schema: type[T], system: str, user: str, *, node: str, deadline: float | None
    ) -> T: ...


PROFILE_BY_NODE = {
    "classify": "gemini_classify",
    "act": "gemini_classify",
    "compose": "gemini_compose",
}
# 기본 모델 1차 시도 제한시간(초) — 실측 중앙값 2초의 약 2~3배
PRIMARY_TIMEOUT = {"classify": 5.0, "act": 5.0, "compose": 7.0}
MIN_FALLBACK_SECONDS = 3.0


class GeminiLLM:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()
        self._models: dict[str, object] = {}

    def _chat(self, model: str, thinking: str | None):
        """노드별 thinking 수준을 달리한 채팅 모델(지연 생성·캐시).

        3.x는 thinking_level, 2.5는 thinking_budget=0(추론 생략)으로 호출한다.
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
            try:
                from langchain_google_genai import ChatGoogleGenerativeAI

                if model.startswith("gemini-3"):
                    extra = {"thinking_level": thinking} if thinking else {}
                elif model.startswith("gemini-2.5"):
                    extra = {"thinking_budget": 0}
                else:
                    extra = {}
                self._models[key] = ChatGoogleGenerativeAI(vertexai=True, **common, **extra)
            except ImportError:
                from langchain_google_vertexai import ChatVertexAI

                self._models[key] = ChatVertexAI(**common)
        return self._models[key]

    async def _once(
        self,
        model: str,
        schema: type[T],
        system: str,
        user: str,
        node: str,
        deadline: float | None,
        *,
        limit: float,
        attempts: int,
    ) -> T:
        thinking = self.s.compose_thinking if node == "compose" else self.s.classify_thinking
        runnable = self._chat(model, thinking).with_structured_output(schema)

        async def call():
            return await runnable.ainvoke([("system", system), ("human", user)])

        try:
            out = await call_with_retry(
                call,
                timeout=limit,
                deadline=deadline,
                attempts=attempts,
                target=f"gemini:{node}:{model}",
            )
        except (ValidationError, ValueError) as e:
            raise StructuredOutputError(str(e)) from e
        if out is None:
            raise StructuredOutputError("empty structured output")
        return out if isinstance(out, schema) else schema.model_validate(out)

    async def structured(
        self, schema: type[T], system: str, user: str, *, node: str, deadline: float | None
    ) -> T:
        profile_timeout, retries = PROFILES[PROFILE_BY_NODE.get(node, "gemini_classify")]
        fallback = self.s.gemini_fallback_model
        try:
            return await self._once(
                self.s.gemini_model,
                schema,
                system,
                user,
                node,
                deadline,
                limit=PRIMARY_TIMEOUT.get(node, 5.0) if fallback else profile_timeout,
                attempts=1 if fallback else retries + 1,
            )
        except CallFailed as first:
            left = remaining(deadline)
            if not fallback or (left is not None and left < MIN_FALLBACK_SECONDS):
                raise _unavailable(first) from first
            try:
                return await self._once(
                    fallback,
                    schema,
                    system,
                    user,
                    node,
                    deadline,
                    limit=profile_timeout,
                    attempts=retries + 1,
                )
            except CallFailed as second:
                raise _unavailable(second) from second


def _unavailable(e: CallFailed) -> LLMUnavailable:
    return LLMTimeout(str(e)) if e.timed_out else LLMUnavailable(str(e))

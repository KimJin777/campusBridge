"""공통 오류 계약(상세설계 04 §7, SSE error 이벤트 04 §2-4)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

ApiErrorCode = Literal[
    "BAD_REQUEST",
    "SCHEMA_MISMATCH",
    "UNAUTHORIZED",
    "THREAD_BUSY",
    "RATE_LIMITED",
    "INTERNAL",
]

HTTP_STATUS: dict[str, int] = {
    "BAD_REQUEST": 400,
    "SCHEMA_MISMATCH": 400,
    "UNAUTHORIZED": 401,
    "THREAD_BUSY": 409,
    "RATE_LIMITED": 429,
    "INTERNAL": 500,
}

# 스트림 시작 후에는 HTTP 상태를 바꿀 수 없으므로 error 이벤트 + done으로 알린다.
StreamErrorCode = Literal["LLM_UNAVAILABLE", "TIMEOUT", "INTERNAL"]

RETRY_MESSAGE = "잠시 후 다시 시도해 주세요."


class ApiError(BaseModel):
    code: ApiErrorCode
    message: str


class StreamError(BaseModel):
    code: StreamErrorCode
    message: str = RETRY_MESSAGE


class AppError(Exception):
    """API 계층에서 ApiError 응답으로 바뀌는 예외."""

    def __init__(self, code: ApiErrorCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    @property
    def status(self) -> int:
        return HTTP_STATUS[self.code]

    def to_model(self) -> ApiError:
        return ApiError(code=self.code, message=self.message)

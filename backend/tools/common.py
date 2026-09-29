"""Shared safety and response helpers for campus data tools."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from backend.domain import ToolResult

TOOL_TIMEOUT_SECONDS = 5.0


def utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_allowed_url(url: str, allowed_hosts: frozenset[str]) -> str:
    """Return *url* when it is HTTPS and points at an explicitly allowed host."""

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or host not in allowed_hosts or parsed.username is not None:
        raise ValueError("허용되지 않은 원문 주소입니다")
    return url


SCHOOL_DOMAIN = "kyungnam.ac.kr"


def is_school_url(url: str) -> bool:
    """https + kyungnam.ac.kr 또는 그 하위 도메인(교수님 2026-09-29: 하위 사이트 수집 허용)."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    return (
        parsed.scheme == "https"
        and parsed.username is None
        and (host == SCHOOL_DOMAIN or host.endswith("." + SCHOOL_DOMAIN))
    )


def truncate(value: str | None, limit: int) -> str:
    return (value or "").strip()[:limit]


def fail_from_exception(exc: BaseException) -> ToolResult:
    if isinstance(exc, TimeoutError):
        return ToolResult.fail("UPSTREAM_TIMEOUT", "원문 조회 시간이 초과되었습니다")
    return ToolResult.fail("UPSTREAM_ERROR", "원문을 조회하지 못했습니다")


def plain_mapping(value: Any) -> dict[str, Any]:
    """Convert protobuf map wrappers without leaking SDK-specific objects."""

    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    try:
        return dict(value)
    except (TypeError, ValueError):
        return {}

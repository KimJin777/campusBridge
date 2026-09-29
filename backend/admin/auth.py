"""Google ID-token authentication for administrator APIs."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from fastapi import Header
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token

from backend.app.config import Settings, get_settings
from backend.domain import AppError


@dataclass(frozen=True)
class AdminActor:
    email: str
    subject: str


def validate_admin_claims(claims: dict[str, Any], settings: Settings) -> AdminActor:
    """Validate authorization claims after signature/audience verification."""

    email = str(claims.get("email") or "").strip().lower()
    verified = claims.get("email_verified") is True or claims.get("email_verified") == "true"
    subject = str(claims.get("sub") or "").strip()
    if not email or not verified or not subject or not settings.is_admin(email):
        raise AppError("UNAUTHORIZED", "관리자 인증이 필요합니다.")
    return AdminActor(email=email, subject=subject)


def _verify_token(token: str, audience: str) -> dict[str, Any]:
    return id_token.verify_oauth2_token(token, GoogleRequest(), audience)


async def require_admin(authorization: str | None = Header(default=None)) -> AdminActor:
    settings = get_settings()
    if not authorization or not authorization.startswith("Bearer "):
        raise AppError("UNAUTHORIZED", "관리자 인증이 필요합니다.")
    token = authorization.removeprefix("Bearer ").strip()
    if not token or not settings.google_oauth_client_id:
        raise AppError("UNAUTHORIZED", "관리자 인증이 필요합니다.")
    try:
        claims = await asyncio.to_thread(
            _verify_token,
            token,
            settings.google_oauth_client_id,
        )
    except Exception as exc:
        raise AppError("UNAUTHORIZED", "관리자 인증이 필요합니다.") from exc
    return validate_admin_claims(claims, settings)

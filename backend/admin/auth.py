"""Google ID-token authentication for administrator APIs."""

from __future__ import annotations

import asyncio
import time
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


_ADMIN_CACHE_TTL_SECONDS = 60.0
_admin_cache_emails: frozenset[str] = frozenset()
_admin_cache_expires_at = 0.0


async def _fetch_active_admin_emails(settings: Settings) -> frozenset[str]:
    """Load dynamic administrators. Callers deliberately fail closed on errors."""

    from google.cloud import firestore

    db = firestore.AsyncClient(
        project=settings.gcp_project_id or None,
        database=settings.firestore_db,
    )
    emails: set[str] = set()
    async for snapshot in db.collection("admin_users").stream():
        row = snapshot.to_dict() or {}
        if row.get("status") != "active":
            continue
        email = str(row.get("email") or snapshot.id).strip().lower()
        if email:
            emails.add(email)
    return frozenset(emails)


async def active_admin_emails(settings: Settings) -> frozenset[str]:
    """Return a short-lived allowlist; lookup failures never retain dynamic access."""

    global _admin_cache_emails, _admin_cache_expires_at
    now = time.monotonic()
    if now < _admin_cache_expires_at:
        return _admin_cache_emails
    try:
        emails = await _fetch_active_admin_emails(settings)
    except Exception:  # noqa: BLE001 — auth lookup failure must fail closed
        _admin_cache_emails = frozenset()
        _admin_cache_expires_at = 0.0
        return frozenset()
    _admin_cache_emails = emails
    _admin_cache_expires_at = now + _ADMIN_CACHE_TTL_SECONDS
    return emails


def invalidate_admin_cache() -> None:
    global _admin_cache_emails, _admin_cache_expires_at
    _admin_cache_emails = frozenset()
    _admin_cache_expires_at = 0.0


def validate_admin_claims(
    claims: dict[str, Any],
    settings: Settings,
    active_emails: frozenset[str] = frozenset(),
) -> AdminActor:
    """Validate authorization claims after signature/audience verification."""

    email = str(claims.get("email") or "").strip().lower()
    verified = claims.get("email_verified") is True or claims.get("email_verified") == "true"
    subject = str(claims.get("sub") or "").strip()
    allowed = settings.is_admin(email) or email in active_emails
    if not email or not verified or not subject or not allowed:
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
    email = str(claims.get("email") or "").strip().lower()
    if settings.is_admin(email):
        return validate_admin_claims(claims, settings)
    dynamic_emails = await active_admin_emails(settings)
    return validate_admin_claims(claims, settings, dynamic_emails)

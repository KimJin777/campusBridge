"""Google ID-token authentication for administrator APIs."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, Header
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token

from backend.app.config import Settings, get_settings
from backend.domain import AppError

SUPER_ADMIN = "super_admin"
ADMIN = "admin"


@dataclass(frozen=True)
class AdminActor:
    email: str
    subject: str
    role: str = ADMIN  # 부트스트랩·super_admin 행 = 최고관리자(관리자 승인·삭제·역할 변경 가능)


@dataclass(frozen=True)
class GoogleUser:
    """관리자 여부와 무관하게 Google 로그인만 확인된 사용자(승인 요청·상태 확인용)."""

    email: str
    subject: str
    name: str = ""


_ADMIN_CACHE_TTL_SECONDS = 60.0
_role_cache: dict[str, str] = {}
_role_cache_expires_at = 0.0
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
    global _admin_cache_emails, _admin_cache_expires_at, _role_cache, _role_cache_expires_at
    _admin_cache_emails = frozenset()
    _admin_cache_expires_at = 0.0
    _role_cache = {}
    _role_cache_expires_at = 0.0


async def _fetch_super_admins(settings: Settings) -> dict[str, str]:
    from google.cloud import firestore

    db = firestore.AsyncClient(
        project=settings.gcp_project_id or None, database=settings.firestore_db
    )
    roles: dict[str, str] = {}
    async for snapshot in db.collection("admin_users").stream():
        row = snapshot.to_dict() or {}
        if row.get("status") == "active" and row.get("role") == SUPER_ADMIN:
            roles[str(row.get("email") or snapshot.id).strip().lower()] = SUPER_ADMIN
    return roles


async def admin_role(settings: Settings, email: str) -> str:
    """부트스트랩은 항상 최고관리자, 화면 등록 관리자는 role 필드.

    조회 실패 시 일반 관리자(최소 권한).
    """
    global _role_cache, _role_cache_expires_at
    if settings.is_admin(email):
        return SUPER_ADMIN
    now = time.monotonic()
    if now >= _role_cache_expires_at:
        try:
            _role_cache = await _fetch_super_admins(settings)
            _role_cache_expires_at = now + _ADMIN_CACHE_TTL_SECONDS
        except Exception:  # noqa: BLE001 — 실패 시 권한 확대 금지
            _role_cache, _role_cache_expires_at = {}, 0.0
    return _role_cache.get(email, ADMIN)


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


async def _claims(authorization: str | None, settings: Settings) -> dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise AppError("UNAUTHORIZED", "관리자 인증이 필요합니다.")
    token = authorization.removeprefix("Bearer ").strip()
    if not token or not settings.google_oauth_client_id:
        raise AppError("UNAUTHORIZED", "관리자 인증이 필요합니다.")
    try:
        return await asyncio.to_thread(_verify_token, token, settings.google_oauth_client_id)
    except Exception as exc:
        raise AppError("UNAUTHORIZED", "관리자 인증이 필요합니다.") from exc


async def require_google_user(authorization: str | None = Header(default=None)) -> GoogleUser:
    """Google 로그인만 확인(관리자 아님도 통과) — 승인 요청·내 상태 조회 전용."""
    claims = await _claims(authorization, get_settings())
    email = str(claims.get("email") or "").strip().lower()
    verified = claims.get("email_verified") is True or claims.get("email_verified") == "true"
    subject = str(claims.get("sub") or "").strip()
    if not email or not verified or not subject:
        raise AppError("UNAUTHORIZED", "Google 로그인이 필요합니다.")
    return GoogleUser(email=email, subject=subject, name=str(claims.get("name") or "")[:50])


async def require_admin(authorization: str | None = Header(default=None)) -> AdminActor:
    settings = get_settings()
    claims = await _claims(authorization, settings)
    email = str(claims.get("email") or "").strip().lower()
    if settings.is_admin(email):
        actor = validate_admin_claims(claims, settings)
    else:
        dynamic_emails = await active_admin_emails(settings)
        actor = validate_admin_claims(claims, settings, dynamic_emails)
    return AdminActor(actor.email, actor.subject, await admin_role(settings, actor.email))


async def require_super_admin(
    actor: AdminActor = Depends(require_admin),  # noqa: B008 — FastAPI 의존성
) -> AdminActor:
    if actor.role != SUPER_ADMIN:
        raise AppError(
            "FORBIDDEN", "최고관리자만 관리자를 승인·삭제하거나 역할을 바꿀 수 있습니다."
        )
    return actor

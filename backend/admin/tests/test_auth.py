from __future__ import annotations

import pytest

from backend.admin import auth
from backend.admin.auth import (
    active_admin_emails,
    invalidate_admin_cache,
    require_admin,
    validate_admin_claims,
)
from backend.app.config import Settings
from backend.domain import AppError


def test_validate_admin_claims_accepts_verified_allowlisted_email() -> None:
    settings = Settings(admin_emails=frozenset({"admin@example.edu"}))

    actor = validate_admin_claims(
        {"email": "ADMIN@example.edu", "email_verified": True, "sub": "google-sub"},
        settings,
    )

    assert actor.email == "admin@example.edu"
    assert actor.subject == "google-sub"


@pytest.mark.parametrize(
    "claims",
    [
        {"email": "admin@example.edu", "email_verified": False, "sub": "sub"},
        {"email": "other@example.edu", "email_verified": True, "sub": "sub"},
        {"email": "admin@example.edu", "email_verified": True},
    ],
)
def test_validate_admin_claims_rejects_untrusted_claims(claims) -> None:
    settings = Settings(admin_emails=frozenset({"admin@example.edu"}))
    with pytest.raises(AppError) as caught:
        validate_admin_claims(claims, settings)
    assert caught.value.code == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_firestore_active_admin_is_cached_for_sixty_seconds(monkeypatch) -> None:
    calls = 0

    async def fake_fetch(settings):
        nonlocal calls
        calls += 1
        return frozenset({"dynamic@example.edu"})

    invalidate_admin_cache()
    monkeypatch.setattr(auth, "_fetch_active_admin_emails", fake_fetch)
    settings = Settings(admin_emails=frozenset())

    first = await active_admin_emails(settings)
    second = await active_admin_emails(settings)
    actor = validate_admin_claims(
        {"email": "DYNAMIC@example.edu", "email_verified": True, "sub": "sub"},
        settings,
        second,
    )

    assert first == second == frozenset({"dynamic@example.edu"})
    assert calls == 1
    assert actor.email == "dynamic@example.edu"


@pytest.mark.asyncio
async def test_firestore_failure_falls_back_to_bootstrap_only(monkeypatch) -> None:
    async def failed_fetch(settings):
        raise RuntimeError("firestore unavailable")

    invalidate_admin_cache()
    monkeypatch.setattr(auth, "_fetch_active_admin_emails", failed_fetch)
    settings = Settings(admin_emails=frozenset({"bootstrap@example.edu"}))
    dynamic = await active_admin_emails(settings)

    assert dynamic == frozenset()
    assert validate_admin_claims(
        {"email": "bootstrap@example.edu", "email_verified": True, "sub": "sub"},
        settings,
        dynamic,
    ).email == "bootstrap@example.edu"
    with pytest.raises(AppError):
        validate_admin_claims(
            {"email": "dynamic@example.edu", "email_verified": True, "sub": "sub"},
            settings,
            dynamic,
        )


@pytest.mark.asyncio
async def test_bootstrap_admin_does_not_depend_on_firestore(monkeypatch) -> None:
    settings = Settings(
        admin_emails=frozenset({"bootstrap@example.edu"}),
        google_oauth_client_id="client-id",
    )
    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    monkeypatch.setattr(
        auth,
        "_verify_token",
        lambda token, audience: {
            "email": "bootstrap@example.edu",
            "email_verified": True,
            "sub": "sub",
        },
    )

    async def should_not_fetch(settings):
        raise AssertionError("bootstrap auth queried Firestore")

    monkeypatch.setattr(auth, "_fetch_active_admin_emails", should_not_fetch)

    actor = await require_admin("Bearer token")

    assert actor.email == "bootstrap@example.edu"

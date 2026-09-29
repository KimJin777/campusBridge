from __future__ import annotations

import pytest

from backend.admin.auth import validate_admin_claims
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

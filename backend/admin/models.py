"""Private request contracts for administrator-only operations."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

_EMAIL = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+"
)


def normalize_admin_email(value: str) -> str:
    email = value.strip().lower()
    local = email.partition("@")[0]
    invalid_dots = local.startswith(".") or local.endswith(".") or ".." in local
    if (
        len(email) > 254
        or len(local) > 64
        or invalid_dots
        or _EMAIL.fullmatch(email) is None
    ):
        raise ValueError("invalid email")
    return email


class AdminAddRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    note: str = Field(default="", max_length=300)
    reason: str = Field(min_length=1, max_length=300)
    request_id: str = Field(min_length=8, max_length=100)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        return normalize_admin_email(value)

    @field_validator("note", "reason", "request_id")
    @classmethod
    def strip_fields(cls, value: str, info) -> str:
        clean = value.strip()
        if info.field_name in {"reason", "request_id"} and not clean:
            raise ValueError("blank value")
        if info.field_name == "request_id" and len(clean) < 8:
            raise ValueError("short request id")
        return clean


class AdminRemoveRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=300)
    request_id: str = Field(min_length=8, max_length=100)

    @field_validator("reason", "request_id")
    @classmethod
    def strip_required(cls, value: str, info) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("blank value")
        if info.field_name == "request_id" and len(clean) < 8:
            raise ValueError("short request id")
        return clean


class SourcePatch(BaseModel):
    schedule: Literal["manual", "daily", "weekly"] | None = None
    paused: bool | None = None
    reason: str = Field(min_length=1, max_length=300)
    request_id: str = Field(min_length=1, max_length=100)

    @field_validator("reason")
    @classmethod
    def reason_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("blank reason")
        return value.strip()

    @model_validator(mode="after")
    def has_mutation(self):
        if self.paused is None and self.schedule is None:
            raise ValueError("schedule or paused required")
        return self


class DisableRequest(BaseModel):
    document_ids: list[str] = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=300)
    request_id: str = Field(min_length=1, max_length=100)

    @field_validator("document_ids")
    @classmethod
    def valid_ids(cls, values: list[str]) -> list[str]:
        clean = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if not clean or any(len(value) > 200 or "/" in value for value in clean):
            raise ValueError("invalid document id")
        return clean

    @field_validator("reason")
    @classmethod
    def reason_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("blank reason")
        return value.strip()


class IngestionRunRequest(BaseModel):
    """Idempotent request to run the incremental ingestion Cloud Run Job."""

    source_ids: list[str] = Field(default_factory=list, max_length=50)
    idempotency_key: str = Field(min_length=8, max_length=100)

    @field_validator("source_ids")
    @classmethod
    def valid_source_ids(cls, values: list[str]) -> list[str]:
        clean = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if any(len(value) > 100 or "/" in value for value in clean):
            raise ValueError("invalid source id")
        return clean

    @field_validator("idempotency_key")
    @classmethod
    def valid_idempotency_key(cls, value: str) -> str:
        clean = value.strip()
        if len(clean) < 8 or "/" in clean:
            raise ValueError("invalid idempotency key")
        return clean


class DocumentActionRequest(BaseModel):
    """교내 문서 상태 전이 요청(publish·reject·archive·purge)."""

    reason: str = Field(min_length=1, max_length=300)
    request_id: str = Field(min_length=8, max_length=100)
    confirm_document_id: str | None = Field(default=None, max_length=100)  # purge 2단계 확인

    @field_validator("reason")
    @classmethod
    def reason_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("blank reason")
        return value.strip()

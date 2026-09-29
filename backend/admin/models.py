"""Private request contracts for administrator-only operations."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


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

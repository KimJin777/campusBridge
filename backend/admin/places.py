"""장소 표 운영(상세설계 01 §4-1, 04 §6, #543 결정: Firestore `places`가 운영 정본).

- 등록은 항상 `pending`으로 시작, 원문 URL(허용 호스트 https)과 확인일이 있어야 `verified`로 전환.
- 검수된 행의 위치 내용을 고치면 다시 `pending`으로 돌아간다(검수 없이 학생에게 노출 금지).
- 지도·좌표는 받지 않는다(교수님 #461).
"""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from backend.app.config import Settings
from backend.domain import AppError
from backend.tools.common import ensure_allowed_url

PLACE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
LOCATION_FIELDS = ("name", "aliases", "kind", "parent_place_id", "floor", "room", "raw_location")


class PlaceFields(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    aliases: list[str] | None = Field(default=None, max_length=20)
    kind: Literal["building", "unit", "facility"] | None = None
    parent_place_id: str | None = Field(default=None, max_length=64)
    floor: str | None = Field(default=None, max_length=10)
    room: str | None = Field(default=None, max_length=20)
    raw_location: str | None = Field(default=None, max_length=120)
    source_url: str | None = Field(default=None, max_length=300)
    snapshot_at: date | None = None
    note: str | None = Field(default=None, max_length=300)

    @field_validator("aliases")
    @classmethod
    def clean_aliases(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        return list(dict.fromkeys(v.strip()[:30] for v in values if v.strip()))


class PlaceCreate(PlaceFields):
    place_id: str
    name: str = Field(min_length=1, max_length=60)
    kind: Literal["building", "unit", "facility"]
    reason: str = Field(min_length=1, max_length=300)
    request_id: str = Field(min_length=8, max_length=100)

    @field_validator("place_id")
    @classmethod
    def valid_place_id(cls, value: str) -> str:
        clean = value.strip().lower()
        if not PLACE_ID.match(clean):
            raise ValueError("place_id must be lowercase letters, digits, _ or -")
        return clean


class PlacePatch(PlaceFields):
    status: Literal["pending", "verified"] | None = None
    reason: str = Field(min_length=1, max_length=300)
    request_id: str = Field(min_length=8, max_length=100)

    @model_validator(mode="after")
    def has_change(self):
        fields = self.model_dump(exclude={"reason", "request_id"}, exclude_none=True)
        if not fields:
            raise ValueError("no change")
        return self


def normalize_place_changes(
    changes: dict[str, object], current: dict[str, object] | None, settings: Settings
) -> dict[str, object]:
    """허용 호스트 검사, 날짜 문자열화, 상태 전이 규칙 적용 후 저장할 필드를 돌려준다."""
    out = {k: v for k, v in changes.items() if v is not None}
    if "snapshot_at" in out:
        out["snapshot_at"] = str(out["snapshot_at"])
    if out.get("source_url"):
        try:
            ensure_allowed_url(str(out["source_url"]), settings.allowed_hosts)
        except ValueError as exc:
            raise AppError("BAD_REQUEST", "원문 주소는 학교 도메인 https여야 합니다.") from exc
    merged = {**(current or {}), **out}
    requested = out.pop("status", None)
    if current is None:
        out["status"] = "pending"
        if requested == "verified":
            raise AppError("BAD_REQUEST", "새 장소는 pending으로 등록한 뒤 검수해 주세요.")
        return out
    location_changed = any(k in out and out[k] != current.get(k) for k in LOCATION_FIELDS)
    if requested == "verified":
        if not merged.get("source_url") or not merged.get("snapshot_at"):
            raise AppError("BAD_REQUEST", "검수 완료에는 원문 주소와 확인일이 필요합니다.")
        if not (merged.get("raw_location") or merged.get("parent_place_id")):
            raise AppError("BAD_REQUEST", "위치(건물·층·호실 또는 위치 문구)가 없습니다.")
        out["status"] = "verified"
    elif requested == "pending" or (location_changed and current.get("status") == "verified"):
        out["status"] = "pending"  # 검수된 위치를 고치면 재검수
    return out

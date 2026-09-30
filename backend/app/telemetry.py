"""화면 오류 수집(운영 관측 #747 — GPT5 #746 엄격안).

- 고정된 오류 '종류'만 받는다. 오류 원문 메시지·스택·URL·입력은 받지 않는다(이름·토큰 유출 방지).
- 공개 엔드포인트라 네트워크별 하루 상한 + 전체 하루 상한. 넘으면 조용히 버린다(항상 204).
- 14일 뒤 Firestore TTL로 삭제(expire_at).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field, field_validator

from backend.app.netutil import request_ip
from backend.reports import rules
from backend.reports.api import Store

KST = timezone(timedelta(hours=9), "KST")
PER_NET_DAY = 30
GLOBAL_DAY = 2000
KEEP_DAYS = 14

router = APIRouter(tags=["telemetry"])


class ClientError(BaseModel):
    kind: Literal["js_error", "unhandled_rejection", "sse_disconnect", "map_load", "calendar_fetch"]
    page: Literal["chat", "tips", "calendar"]
    app_version: str = Field(default="", max_length=20)
    fp: str = Field(default="", max_length=16)  # 같은 오류 묶음용 짧은 지문(원문 아님)

    @field_validator("app_version")
    @classmethod
    def _version(cls, v: str) -> str:
        return v if re.fullmatch(r"[0-9.]{0,20}", v) else ""

    @field_validator("fp")
    @classmethod
    def _fp(cls, v: str) -> str:
        return v if re.fullmatch(r"[0-9a-z]{0,16}", v) else ""


@router.post("/api/client-error", status_code=204)
async def client_error(body: ClientError, request: Request, store: Store) -> Response:
    day = datetime.now(KST).date().isoformat()
    net = rules.net_hash(await store.secret(), request_ip(request), day)
    ok = await store.take_quota(f"cerr_{net}", PER_NET_DAY, day) and await store.take_quota(
        "cerr_all", GLOBAL_DAY, day
    )
    if ok:
        now = datetime.now(UTC)
        await store.add_client_error(
            {**body.model_dump(), "created_at": now, "expire_at": now + timedelta(days=KEEP_DAYS)}
        )
    return Response(status_code=204)

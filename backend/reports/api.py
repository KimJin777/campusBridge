# ruff: noqa: E501 — 안내 문구·정규식은 줄바꿈하지 않는다
"""익명 제보·꿀팁 API(교수님 #697~#715, 착수 2026-09-30).

학생
- POST /api/reports/tip          꿀팁 제보 → 사전검사·에이전트 판정 → 검증 중(공개 투표) / 관리자 확인 / 반려
- POST /api/reports/wrong-info   답변 오류 제보 → 스냅샷 보존 → 결정적 모순이면 자동 '확인됨', 아니면 관리자 확인
- GET  /api/tips                 검증 중·승인된 꿀팁(내 투표 포함)
- POST /api/tips/{id}/vote       맞아요(confirm) / 사실과 달라요(dispute) / 문제 신고(flag)
관리자
- GET  /api/admin/reports?type=tip|wrong_info
- POST /api/admin/reports/{id}/{action}

개인정보: 로그인 없음. 본문은 마스킹 후에만 저장, IP는 일 단위 HMAC만, 투표 토큰은 해시만 저장.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import re
from datetime import UTC, datetime, timedelta, timezone
from functools import lru_cache
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from backend.admin.auth import AdminActor, require_admin
from backend.app.config import Settings, get_settings
from backend.domain import AppError
from backend.reports import rules
from backend.reports.store import FirestoreReportStore, MemoryReportStore, ReportStore, mine
from backend.reports.triage import deterministic_mismatch, judge_report, judge_tip
from backend.store.mask import mask

KST = timezone(timedelta(hours=9), "KST")
VOTE_COOKIE = "cb_vt"
VOTE_COOKIE_DAYS = 90
TURN_ID = re.compile(r"^[0-9a-f-]{36}_[0-9a-f-]{36}$")
PUBLIC_TIP_STATUSES = ("verifying", "student_approved", "approved")
REJECTED_TTL = timedelta(days=90)  # 반려 제보는 90일 뒤 Firestore TTL로 삭제

router = APIRouter(tags=["reports"])


@lru_cache(maxsize=1)
def _firestore_store() -> FirestoreReportStore:
    return FirestoreReportStore(get_settings())


_memory = MemoryReportStore()


def get_report_store(settings: Annotated[Settings, Depends(get_settings)]) -> ReportStore:
    return _firestore_store() if settings.gcp_project_id else _memory


@lru_cache(maxsize=1)
def _gemini() -> Any:
    from backend.agent.llm import GeminiLLM

    return GeminiLLM(get_settings())


def get_report_llm() -> Any:
    return _gemini()


Store = Annotated[ReportStore, Depends(get_report_store)]
LLM = Annotated[Any, Depends(get_report_llm)]


# 구글 프런트엔드·부하분산 프록시 대역(이 주소는 사용자가 아님)
GOOGLE_PROXIES = tuple(ipaddress.ip_network(n) for n in ("35.191.0.0/16", "130.211.0.0/22"))


def _trusted_proxy(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or any(ip in net for net in GOOGLE_PROXIES if ip.version == net.version)
    )


def client_ip(xff: str | None, peer: str | None) -> str:
    """X-Forwarded-For를 오른쪽부터 보며 구글 프록시·사설망을 건너뛴 첫 공인 주소(GPT5 #722-1).

    'client, google-proxy' → client, '위조값, client' → client(오른쪽이 우리 쪽에서 붙인 값).
    """
    for part in reversed([p.strip() for p in (xff or "").split(",") if p.strip()]):
        try:
            ip = ipaddress.ip_address(part)
        except ValueError:
            continue
        if not _trusted_proxy(ip):
            return str(ip)
    return peer or "unknown"


def _client_ip(request: Request) -> str:
    return client_ip(
        request.headers.get("x-forwarded-for"), request.client.host if request.client else None
    )


def _day() -> str:
    return datetime.now(KST).date().isoformat()


class TipSubmit(BaseModel):
    category: Literal["place", "facility", "life"]
    text: str = Field(min_length=1, max_length=rules.MAX_TIP_CHARS)
    website: str = ""  # 숨은 입력칸(사람은 비워 둔다)
    elapsed_ms: int = 0


class WrongInfoSubmit(BaseModel):
    turn_id: str = Field(max_length=80)
    text: str = Field(default="", max_length=rules.MAX_REPORT_CHARS)
    website: str = ""
    elapsed_ms: int = 0


class VoteRequest(BaseModel):
    value: Literal["confirm", "dispute", "flag"]
    reason: Literal["privacy", "abuse", "safety", "wrong"] | None = None


class AdminAction(BaseModel):
    reason: str = Field(default="", max_length=300)
    text: str | None = Field(default=None, max_length=rules.MAX_TIP_CHARS)  # 다듬어 승인


def _bot(elapsed_ms: int, website: str) -> dict[str, Any] | None:
    """숨은 입력칸이 채워지면 자동화로 보고 조용히 버린다. 너무 빠르면 다시 보내 달라고 한다."""
    if website.strip():
        return {"accepted": True}
    if elapsed_ms < rules.MIN_ELAPSED_MS:
        return {"accepted": False, "message": "너무 빨리 보냈습니다. 잠시 후 다시 보내 주세요."}
    return None


async def _net(store: ReportStore, request: Request) -> str:
    return rules.net_hash(await store.secret(), _client_ip(request), _day())


async def _limits(store: ReportStore, net: str, kind: str, per_net: int) -> None:
    day = _day()
    if not await store.take_quota(f"{kind}_{net}", per_net, day):
        raise AppError("RATE_LIMITED", "오늘은 제보를 더 받을 수 없습니다. 내일 다시 보내 주세요.")
    if not await store.take_quota("all_submit", rules.SUBMIT_GLOBAL_DAY, day):
        raise AppError(
            "RATE_LIMITED", "오늘 제보가 많아 잠시 받지 않습니다. 내일 다시 보내 주세요."
        )


def _receipt() -> tuple[dict[str, str], str | None]:
    """이벤트용 제보 번호. 지금은 이벤트를 하지 않으므로 발급·저장하지 않는다(교수님 2026-09-30).

    REPORT_EVENT_RECEIPTS=1로 켜면 번호를 보여 주고 해시만 저장한다(설계 여지).
    """
    if os.getenv("REPORT_EVENT_RECEIPTS", "0") != "1":
        return {}, None
    shown, rhash = rules.new_receipt()
    return {"receipt": shown}, rhash


def _rejected(reason: str) -> dict[str, Any]:
    return {"accepted": False, "message": f"접수하지 않았습니다: {reason}"}


# ── 꿀팁 제보 ───────────────────────────────────────────────────────────
@router.post("/api/reports/tip")
async def submit_tip(body: TipSubmit, request: Request, store: Store, llm: LLM) -> dict[str, Any]:
    if (bot := _bot(body.elapsed_ms, body.website)) is not None:
        return bot
    net = await _net(store, request)
    await _limits(store, net, "tip", rules.TIP_PER_NET_DAY)
    # 연락처·링크 검사는 마스킹 전 원문으로(메모리에서만), 저장·모델에는 마스킹본만(GPT5 #722-8)
    screen = rules.screen_tip(rules.normalize(body.text))
    text = rules.normalize(mask(body.text))
    now = datetime.now(UTC)
    base = {"type": "tip", "category": body.category, "created_at": now, "net": net}
    if screen.verdict == "reject":
        await store.create(
            {
                **base,
                "status": "rejected",
                "expire_at": now + REJECTED_TTL,
                "decided_by": "rules",
                "reason": screen.reason,
                "text_masked": text if screen.keep_body else None,
            }
        )
        return _rejected(screen.reason)
    if not await store.claim_text(rules.text_hash(text)):
        return _rejected("이미 같은 내용이 제보되었습니다")
    judge = (
        await judge_tip(llm, rules.TIP_CATEGORIES[body.category], text)
        if (screen.verdict == "ok")
        else None
    )
    if judge and judge.verdict == "reject":
        await store.create(
            {
                **base,
                "status": "rejected",
                "expire_at": now + REJECTED_TTL,
                "decided_by": "agent",
                "reason": judge.reason,
                "text_masked": text,
            }
        )
        return _rejected(judge.reason)
    shown, rhash = _receipt()
    public = screen.verdict == "ok" and judge is not None and judge.verdict == "ok"
    doc = {
        **base,
        "status": "verifying" if public else "pending",
        "text_masked": text,
        "receipt_hash": rhash,
        "campaign": None,  # 이벤트 코드(교수님: 나중 이벤트용 여지만)
        "safety": rules.is_safety(text),
        "agent_reason": (judge.reason if judge else screen.reason or "에이전트 판정 실패"),
        "confirm": 0,
        "dispute": 0,
        "flags": 0,
        "net_counts": {},
    }
    if public:
        doc["published_at"] = now
    await store.create(doc)
    return {
        "accepted": True,
        **shown,
        "status": doc["status"],
        "message": "검증 중인 꿀팁에 올라갔습니다. 학생들의 확인을 받으면 승인됩니다."
        if public
        else "관리자 확인 후 공개됩니다.",
    }


# ── 잘못된 정보 제보 ────────────────────────────────────────────────────
def _answer_text(payload: dict[str, Any]) -> str:
    ans = payload.get("answer") or payload.get("fallback") or {}
    parts = [
        s.get("text", "")
        for k in ("sentences", "checklist", "next_actions")
        for s in (ans.get(k) or [])
        if isinstance(s, dict)
    ]
    if isinstance(ans.get("message"), str):
        parts.append(ans["message"])
    return "\n".join(p for p in parts if p)


async def _auto_followups(
    store: ReportStore, rid: str, turn_id: str, doc: dict[str, Any], snapshot: dict, now: datetime
) -> None:
    """자동 '확인됨' 후속: turn 무효 표시 + 평가 후보. 둘 다 멱등(같은 ID로 덮어쓰기)."""
    await store.mark_turn(turn_id, rid)
    await store.add_eval_candidate(
        rid,
        {
            "status": "candidate",
            "created_at": now,
            "question_masked": snapshot["question_masked"],
            "wrong_claim": doc["answer_claim"],
            "source_value": doc["evidence_value"],
            "cited_ids": snapshot["cited_ids"],
            "versions": snapshot["versions"],
        },
    )
    await store.update(rid, {"followups": "done"})


@router.post("/api/reports/wrong-info")
async def submit_wrong_info(
    body: WrongInfoSubmit, request: Request, store: Store, llm: LLM
) -> dict[str, Any]:
    if (bot := _bot(body.elapsed_ms, body.website)) is not None:
        return bot
    if not TURN_ID.match(body.turn_id):
        raise AppError("BAD_REQUEST", "제보할 답변을 찾지 못했습니다.")
    turn = await store.turn(body.turn_id)
    if not turn or not turn.get("final_payload"):
        raise AppError("BAD_REQUEST", "제보할 답변을 찾지 못했습니다.")
    net = await _net(store, request)
    await _limits(store, net, "report", rules.REPORT_PER_NET_DAY)
    screen = rules.screen_report(rules.normalize(body.text))  # 원문 검사, 저장은 마스킹본
    text = rules.normalize(mask(body.text))
    now = datetime.now(UTC)
    payload = turn.get("final_payload") or {}
    quotes = turn.get("cited_quotes") or {}
    evidence_text = "\n\n".join(quotes.values()) or "\n".join(
        c.get("snippet", "") for c in payload.get("cards", [])
    )
    snapshot = {  # 제보 시점의 질문·답변·인용 원문·버전(나중에 원문이 바뀌어도 비교 가능)
        "turn_id": body.turn_id,
        "question_masked": turn.get("query_masked"),
        "outcome": payload.get("outcome"),
        "answer_text": _answer_text(payload),
        "cited_ids": turn.get("cited_ids") or [],
        "cited_quotes": quotes,
        "cards": [{k: c.get(k) for k in ("id", "title", "url")} for c in payload.get("cards", [])],
        "versions": {k: turn.get(k) for k in ("app_version", "prompt_version", "model")},
    }
    base = {"type": "wrong_info", "created_at": now, "net": net, "snapshot": snapshot}
    if screen.verdict == "reject":
        await store.create(
            {
                **base,
                "status": "rejected",
                "expire_at": now + REJECTED_TTL,
                "decided_by": "rules",
                "reason": screen.reason,
                "text_masked": text if screen.keep_body else None,
            }
        )
        return _rejected(screen.reason)
    judge = await judge_report(
        llm, text, snapshot["question_masked"] or "", snapshot["answer_text"], evidence_text
    )
    auto = judge is not None and deterministic_mismatch(
        judge, snapshot["answer_text"], evidence_text
    )
    shown, rhash = _receipt()
    doc = {
        **base,
        "status": "confirmed" if auto else "pending",
        "decided_by": "agent" if auto else None,
        "text_masked": text,
        "receipt_hash": rhash,
        "campaign": None,
        "kind": judge.kind if judge else None,
        "answer_claim": judge.answer_claim if judge else None,
        "evidence_value": judge.evidence_value if judge else None,
        "agent_reason": judge.reason if judge else "에이전트 판정 실패 — 관리자 확인",
    }
    rid = await store.create(doc)
    if auto:
        try:  # 후속 처리(멱등: 평가 후보 ID = 제보 ID). 실패하면 관리자 확인으로 되돌린다
            await _auto_followups(store, rid, body.turn_id, doc, snapshot, now)
        except Exception:  # noqa: BLE001
            await store.update(
                rid,
                {
                    "status": "pending",
                    "decided_by": None,
                    "agent_reason": "자동 확정 후속 처리 실패 — 관리자 확인",
                },
            )
            auto = False
    return {
        "accepted": True,
        **shown,
        "message": "답변과 근거가 다른 부분을 확인했습니다. 관리자가 고칩니다."
        if auto
        else "관리자가 확인합니다.",
    }


# ── 꿀팁 목록·투표 ──────────────────────────────────────────────────────
def _sign(secret: bytes, tid: str) -> str:
    return hmac.new(secret, f"vote|{tid}".encode(), hashlib.sha256).hexdigest()[:32]


async def _vote_token(request: Request, response: Response, store: ReportStore) -> str:
    """서버가 서명한 투표 토큰(id.서명)만 인정. 위조·형식 오류는 새로 발급(GPT5 #722-2)."""
    secret = await store.secret()
    raw = request.cookies.get(VOTE_COOKIE) or ""
    tid, _, sig = raw.partition(".")
    if re.fullmatch(r"[A-Za-z0-9_-]{32,60}", tid) and hmac.compare_digest(sig, _sign(secret, tid)):
        return tid
    tid = rules.new_vote_token()
    response.set_cookie(
        VOTE_COOKIE,
        f"{tid}.{_sign(secret, tid)}",
        max_age=VOTE_COOKIE_DAYS * 86400,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/api/tips",
    )
    return tid


def _public_tip(row: dict[str, Any], mine: dict[str, Any] | None) -> dict[str, Any]:
    confirm, dispute = int(row.get("confirm", 0)), int(row.get("dispute", 0))
    created = row.get("created_at")
    return {
        "id": row["id"],
        "category": rules.TIP_CATEGORIES.get(row.get("category", ""), ""),
        "text": row.get("published_text") or row.get("text_masked") or "",
        "status": row.get("status"),
        "badge": rules.public_badge(str(row.get("status"))),
        "confirm": confirm,
        "dispute": dispute,
        "contested": bool(row.get("contested")),
        "safety": bool(row.get("safety")),
        "stale": row.get("status") in ("approved", "student_approved")
        and not rules.chat_eligible(row, datetime.now(UTC)),
        "created": created.astimezone(KST).date().isoformat()
        if isinstance(created, datetime)
        else None,
        "mine": mine,
    }


@router.get("/api/tips")
async def list_tips(request: Request, response: Response, store: Store) -> dict[str, Any]:
    token = rules.token_hash(await _vote_token(request, response, store))
    rows = await store.list(type_="tip", statuses=PUBLIC_TIP_STATUSES)
    votes = await store.my_votes(token, [r["id"] for r in rows])
    items = [_public_tip(r, mine(votes[r["id"]], r) if r["id"] in votes else None) for r in rows]
    items.sort(key=lambda t: t["created"] or "", reverse=True)
    return {
        "verifying": [t for t in items if t["status"] == "verifying"],
        "approved": [t for t in items if t["status"] in ("approved", "student_approved")],
        "rule": {
            "min_votes": rules.MIN_VOTES,
            "min_ratio": rules.MIN_CONFIRM_RATIO,
            "min_hours": rules.MIN_PUBLIC_HOURS,
        },
    }


@router.post("/api/tips/{tip_id}/vote")
async def vote_tip(
    tip_id: str, body: VoteRequest, request: Request, response: Response, store: Store
) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9]{8,40}", tip_id):
        raise AppError("BAD_REQUEST", "꿀팁을 찾지 못했습니다.")
    token = rules.token_hash(await _vote_token(request, response, store))
    tip = await store.get(tip_id)
    if not tip or tip.get("type") != "tip" or tip.get("status") not in PUBLIC_TIP_STATUSES:
        raise AppError("BAD_REQUEST", "투표할 수 없는 꿀팁입니다.")
    net = await _net(store, request)
    day = _day()
    if not await store.take_quota(f"vote_{token}", rules.VOTES_PER_TOKEN_DAY, day):
        raise AppError("RATE_LIMITED", "오늘은 투표를 더 할 수 없습니다.")
    # 쿠키를 지워 새 토큰을 받아도 네트워크 단위 하루 상한은 넘지 못한다(토큰 양산 방지)
    if not await store.take_quota(f"votenet_{net}", rules.VOTES_PER_NET_DAY, day):
        raise AppError("RATE_LIMITED", "오늘은 이 네트워크에서 투표를 더 할 수 없습니다.")
    reason = (body.reason or "other") if body.value == "flag" else None
    updated = await store.vote(tip_id, token, net, body.value, reason, datetime.now(UTC))
    if updated is None:
        raise AppError("BAD_REQUEST", "꿀팁을 찾지 못했습니다.")
    return _public_tip(updated, updated.get("mine"))


# ── 관리자 제보함 ───────────────────────────────────────────────────────
Actor = Annotated[AdminActor, Depends(require_admin)]
TIP_ACTIONS = {
    "approve": "approved",
    "to_vote": "verifying",
    "reject": "rejected",
    "hide": "hidden",
    "restore": None,
    "withdraw": "withdrawn",
}
REPORT_ACTIONS = {
    "confirm": "confirmed",
    "no_issue": "no_issue",
    "needs_source_review": "needs_source_review",
    "resolve": "resolved",
    "reject": "rejected",
}
ADMIN_FIELDS = (
    "type",
    "status",
    "category",
    "text_masked",
    "published_text",
    "agent_reason",
    "reason",
    "decided_by",
    "safety",
    "contested",
    "burst_risk",
    "confirm",
    "dispute",
    "flags",
    "flag_reasons",
    "kind",
    "answer_claim",
    "evidence_value",
    "snapshot",
    "created_at",
    "published_at",
    "approved_at",
    "approved_by",
    "reviewed_by",
    "reviewed_at",
    "admin_note",
)


def _admin_view(row: dict[str, Any], now: datetime) -> dict[str, Any]:
    view = {"id": row["id"], **{k: row.get(k) for k in ADMIN_FIELDS if k in row}}
    days = rules.RECHECK_DAYS.get(str(row.get("status")))
    approved = row.get("approved_at")
    view["recheck"] = bool(
        days and isinstance(approved, datetime) and now - approved > timedelta(days=days)
    )
    return view


@router.get("/api/admin/reports")
async def admin_list_reports(
    actor: Actor, store: Store, type: Literal["tip", "wrong_info"] = "tip"
) -> dict[str, Any]:
    del actor
    now = datetime.now(UTC)
    rows = [_admin_view(r, now) for r in await store.list(type_=type)]
    order = {"pending": 0, "needs_source_review": 1, "verifying": 2, "confirmed": 3}
    rows.sort(key=lambda r: (order.get(str(r.get("status")), 5), str(r.get("created_at"))))
    return {"items": rows}


@router.post("/api/admin/reports/{report_id}/{action}")
async def admin_report_action(
    report_id: str, action: str, body: AdminAction, actor: Actor, store: Store
) -> dict[str, Any]:
    row = await store.get(report_id) if re.fullmatch(r"[A-Za-z0-9]{8,40}", report_id) else None
    if not row:
        raise AppError("BAD_REQUEST", "제보를 찾지 못했습니다.")
    actions = TIP_ACTIONS if row.get("type") == "tip" else REPORT_ACTIONS
    if action not in actions:
        raise AppError("BAD_REQUEST", "지원하지 않는 처리입니다.")
    now = datetime.now(UTC)
    status = actions[action] or row.get("hidden_from") or "verifying"
    change: dict[str, Any] = {
        "status": status,
        "reviewed_by": actor.email,
        "reviewed_at": now,
        "admin_note": body.reason.strip() or None,
    }
    if action == "approve":
        change.update({"approved_at": now, "approved_by": actor.email})
        if body.text and body.text.strip():
            edited = rules.normalize(mask(body.text))
            if rules.screen_tip(edited).verdict == "reject":
                raise AppError("BAD_REQUEST", "다듬은 문장이 등록 기준에 맞지 않습니다.")
            change["published_text"] = edited
    if action == "to_vote":
        change["published_at"] = now
    if action == "hide":
        change["hidden_from"] = row.get("status")
    if (
        row.get("type") == "tip"
        and action in ("restore", "approve", "to_vote")
        and row.get("flags")
    ):
        # 관리자가 판단을 끝낸 신고는 소진: 세대를 올려 같은 신고로 다시 가려지지 않게 한다
        change.update(
            {
                "flags": 0,
                "flag_gen": int(row.get("flag_gen", 0)) + 1,
                "flag_reasons": {},
                "resolved_flags": row.get("flag_reasons") or {},
            }
        )
    if status == "rejected":
        change["expire_at"] = now + REJECTED_TTL
    await store.update(report_id, change)
    await store.audit(
        {
            "actor": actor.email,
            "actor_sub": actor.subject,
            "action": f"report.{action}",
            "target": report_id,
            "before": {"status": row.get("status")},
            "after": {"status": status},
            "reason": body.reason.strip() or action,
            "request_id": f"report-{report_id}-{now.timestamp():.0f}",
            "result": "success",
            "created_at": now,
        }
    )
    return _admin_view({**row, **change}, now)

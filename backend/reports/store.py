# ruff: noqa: E501 — 설명 문구는 줄바꿈하지 않는다
"""제보·꿀팁·투표 저장소. 운영은 Firestore, 테스트는 메모리.

컬렉션
- reports/{id}: type=tip|wrong_info, status, text_masked(원문 저장 안 함) …
- report_votes/{tip_id}_{token_hash}: 한 토큰의 '투표(ballot)'와 '문제 신고(flag)'를 따로 보관(GPT5 #722-3)
    ballot: confirm|dispute|None, ballot_net(투표 당시 네트워크), flag: 사유|None, flag_gen(신고 세대)
- report_quota/{day}_{key}: 하루 한도 카운터(트랜잭션)
- report_texts/{text_hash}: 중복 본문 차단
- report_config/secret: 네트워크 HMAC·투표 토큰 서명 비밀값(서버만 읽음)
- eval_candidates/{report_id}: 확인된 오류 제보의 평가 후보(관리자 확인 후 평가셋으로)
보관: expire_at 필드 + Firestore TTL(한도 3일, 중복 90일, 투표 180일, 반려 제보 90일)
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from backend.reports.rules import evaluate_tip

QUOTA_TTL = timedelta(days=3)
TEXT_TTL = timedelta(days=90)
VOTE_TTL = timedelta(days=180)


class ReportStore(Protocol):
    async def secret(self) -> bytes: ...
    async def take_quota(self, key: str, limit: int, day: str) -> bool: ...
    async def claim_text(self, text_hash: str) -> bool: ...
    async def create(self, doc: dict[str, Any]) -> str: ...
    async def get(self, report_id: str) -> dict[str, Any] | None: ...
    async def update(self, report_id: str, fields: dict[str, Any]) -> None: ...
    async def list(self, *, type_: str, statuses: tuple[str, ...] | None = None) -> list[dict]: ...
    async def vote(
        self, tip_id: str, token: str, net: str, action: str, reason: str | None, now: datetime
    ) -> dict[str, Any] | None: ...
    async def my_votes(self, token: str, tip_ids: list[str]) -> dict[str, dict[str, Any]]:
        """{tip_id: 투표 문서 원본} — 화면 표시는 mine()으로."""
        ...

    async def turn(self, turn_id: str) -> dict[str, Any] | None: ...
    async def mark_turn(self, turn_id: str, report_id: str) -> None: ...
    async def add_eval_candidate(self, report_id: str, doc: dict[str, Any]) -> None: ...
    async def audit(self, doc: dict[str, Any]) -> None: ...


def apply_vote(
    tip: dict[str, Any],
    vote: dict[str, Any] | None,
    action: str,
    token: str,
    net: str,
    reason: str | None,
    now: datetime,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """(꿀팁 변경분, 새 투표 문서). 변화가 없으면 ({}, None).

    - 투표(confirm/dispute)와 신고(flag)는 서로 지우지 않는다.
    - 투표 변경 시 '투표 당시 네트워크'에서 빼고 현재 네트워크에 더한다(합계 = 유효 표 수).
    - confirm_at: 현재 유효한 '맞아요' 표의 시각만 보관(철회하면 제거) — 챗봇 신선도 계산용.
    - 신고는 현재 신고 세대(flag_gen)에서 토큰당 1회. 관리자 복구 시 세대를 올려 기존 신고를 소진.
    """
    vote = dict(vote or {})
    change: dict[str, Any] = {}
    key = token[:16]
    if action in ("confirm", "dispute"):
        prev, prev_net = vote.get("ballot"), vote.get("ballot_net")
        if prev == action:
            return {}, None
        counts = {k: int(tip.get(k, 0)) for k in ("confirm", "dispute")}
        nets = dict(tip.get("net_counts") or {})
        confirm_at = dict(tip.get("confirm_at") or {})
        if prev in counts:
            counts[prev] = max(0, counts[prev] - 1)
            if prev_net:
                nets[prev_net] = max(0, nets.get(prev_net, 1) - 1)
            confirm_at.pop(key, None)
        counts[action] += 1
        nets[net] = nets.get(net, 0) + 1
        if action == "confirm":
            confirm_at[key] = now
        change = {
            **counts,
            "net_counts": {k: v for k, v in nets.items() if v},
            "confirm_at": confirm_at,
        }
        vote.update({"ballot": action, "ballot_net": net, "ballot_at": now})
    elif action == "flag":
        gen = int(tip.get("flag_gen", 0))
        if vote.get("flag") and int(vote.get("flag_gen", -1)) == gen:
            return {}, None
        reasons = dict(tip.get("flag_reasons") or {})
        reasons[reason or "other"] = reasons.get(reason or "other", 0) + 1
        flag_nets = dict(tip.get("flag_nets") or {})
        flag_nets[net] = flag_nets.get(net, 0) + 1
        change = {
            "flags": int(tip.get("flags", 0)) + 1,
            "flag_reasons": reasons,
            "flag_nets": flag_nets,
        }
        vote.update({"flag": reason or "other", "flag_gen": gen, "flag_at": now, "flag_net": net})
    else:
        return {}, None
    merged = {**tip, **change}
    change.update(evaluate_tip(merged, now))
    vote.update({"expire_at": now + VOTE_TTL})
    return change, vote


def mine(vote: dict[str, Any] | None, tip: dict[str, Any]) -> dict[str, Any]:
    vote = vote or {}
    flagged = bool(vote.get("flag")) and int(vote.get("flag_gen", -1)) == int(
        tip.get("flag_gen", 0)
    )
    return {"ballot": vote.get("ballot"), "flagged": flagged}


class MemoryReportStore:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.votes: dict[str, dict[str, Any]] = {}
        self.quota: dict[str, int] = {}
        self.texts: set[str] = set()
        self.turns: dict[str, dict[str, Any]] = {}
        self.eval: dict[str, dict[str, Any]] = {}
        self.audits: list[dict[str, Any]] = []
        self._secret = b"test-secret"

    async def secret(self) -> bytes:
        return self._secret

    async def take_quota(self, key: str, limit: int, day: str) -> bool:
        k = f"{day}_{key}"
        if self.quota.get(k, 0) >= limit:
            return False
        self.quota[k] = self.quota.get(k, 0) + 1
        return True

    async def claim_text(self, text_hash: str) -> bool:
        if text_hash in self.texts:
            return False
        self.texts.add(text_hash)
        return True

    async def create(self, doc: dict[str, Any]) -> str:
        rid = uuid.uuid4().hex[:20]
        self.rows[rid] = dict(doc)
        return rid

    async def get(self, report_id: str) -> dict[str, Any] | None:
        row = self.rows.get(report_id)
        return {"id": report_id, **row} if row else None

    async def update(self, report_id: str, fields: dict[str, Any]) -> None:
        self.rows[report_id].update(fields)

    async def list(self, *, type_: str, statuses: tuple[str, ...] | None = None) -> list[dict]:
        return [
            {"id": k, **v}
            for k, v in self.rows.items()
            if v.get("type") == type_ and (statuses is None or v.get("status") in statuses)
        ]

    async def vote(self, tip_id, token, net, action, reason, now):
        tip = self.rows.get(tip_id)
        if not tip or tip.get("type") != "tip":
            return None
        key = f"{tip_id}_{token}"
        change, new_vote = apply_vote(tip, self.votes.get(key), action, token, net, reason, now)
        if new_vote is not None:
            self.votes[key] = new_vote
        tip.update(change)
        return {"id": tip_id, **tip, "mine": mine(self.votes.get(key), tip)}

    async def my_votes(self, token, tip_ids):
        return {t: self.votes[f"{t}_{token}"] for t in tip_ids if f"{t}_{token}" in self.votes}

    async def turn(self, turn_id):
        return self.turns.get(turn_id)

    async def mark_turn(self, turn_id, report_id):
        self.turns.setdefault(turn_id, {}).update(
            {"invalidated_report_id": report_id, "invalidated_at": datetime.now(UTC)}
        )

    async def add_eval_candidate(self, report_id, doc):
        self.eval[report_id] = doc

    async def audit(self, doc):
        self.audits.append(doc)


class FirestoreReportStore:
    def __init__(self, settings: Any):
        from google.cloud import firestore

        self._fs = firestore
        self.db = firestore.AsyncClient(
            project=settings.gcp_project_id or None, database=settings.firestore_db
        )
        self._secret: bytes | None = None

    async def secret(self) -> bytes:
        if self._secret is None:
            from google.api_core.exceptions import AlreadyExists

            ref = self.db.collection("report_config").document("secret")
            snap = await ref.get()
            value = (snap.to_dict() or {}).get("hmac") if snap.exists else None
            if not value:
                try:
                    await ref.create(
                        {"hmac": secrets.token_hex(32), "created_at": datetime.now(UTC)}
                    )
                except AlreadyExists:  # 다른 인스턴스가 먼저 만들었으면 그 값을 쓴다
                    pass
                value = ((await ref.get()).to_dict() or {})["hmac"]
            self._secret = bytes.fromhex(value)
        return self._secret

    async def take_quota(self, key: str, limit: int, day: str) -> bool:
        ref = self.db.collection("report_quota").document(f"{day}_{key}")

        @self._fs.async_transactional
        async def txn(tx) -> bool:
            snap = await ref.get(transaction=tx)
            n = int((snap.to_dict() or {}).get("n", 0)) if snap.exists else 0
            if n >= limit:
                return False
            now = datetime.now(UTC)
            tx.set(ref, {"n": n + 1, "day": day, "updated_at": now, "expire_at": now + QUOTA_TTL})
            return True

        return await txn(self.db.transaction())

    async def claim_text(self, text_hash: str) -> bool:
        from google.api_core.exceptions import AlreadyExists

        now = datetime.now(UTC)
        try:
            await (
                self.db.collection("report_texts")
                .document(text_hash)
                .create({"at": now, "expire_at": now + TEXT_TTL})
            )
            return True
        except AlreadyExists:
            return False

    async def create(self, doc: dict[str, Any]) -> str:
        ref = self.db.collection("reports").document()
        await ref.set(doc)
        return ref.id

    async def get(self, report_id: str) -> dict[str, Any] | None:
        snap = await self.db.collection("reports").document(report_id).get()
        return {"id": snap.id, **(snap.to_dict() or {})} if snap.exists else None

    async def update(self, report_id: str, fields: dict[str, Any]) -> None:
        await self.db.collection("reports").document(report_id).set(fields, merge=True)

    async def list(self, *, type_: str, statuses: tuple[str, ...] | None = None) -> list[dict]:
        from google.cloud.firestore_v1.base_query import FieldFilter

        q = self.db.collection("reports").where(filter=FieldFilter("type", "==", type_))
        rows = [{"id": d.id, **(d.to_dict() or {})} async for d in q.limit(500).stream()]
        return [r for r in rows if statuses is None or r.get("status") in statuses]

    async def vote(self, tip_id, token, net, action, reason, now):
        tip_ref = self.db.collection("reports").document(tip_id)
        vote_ref = self.db.collection("report_votes").document(f"{tip_id}_{token}")

        @self._fs.async_transactional
        async def txn(tx):
            tip_snap = await tip_ref.get(transaction=tx)
            tip = (tip_snap.to_dict() or {}) if tip_snap.exists else {}
            if tip.get("type") != "tip":
                return None
            vote_snap = await vote_ref.get(transaction=tx)
            vote = vote_snap.to_dict() if vote_snap.exists else None
            change, new_vote = apply_vote(tip, vote, action, token, net, reason, now)
            if change:
                tx.set(tip_ref, {**change, "updated_at": now}, merge=True)
            if new_vote is not None:
                tx.set(vote_ref, {**new_vote, "tip_id": tip_id})
            tip.update(change)
            return {"id": tip_id, **tip, "mine": mine(new_vote or vote, tip)}

        return await txn(self.db.transaction())

    async def my_votes(self, token, tip_ids):
        refs = [self.db.collection("report_votes").document(f"{t}_{token}") for t in tip_ids]
        rows = [s.to_dict() or {} async for s in self.db.get_all(refs) if s.exists]
        return {r.get("tip_id", ""): r for r in rows}

    async def turn(self, turn_id):
        snap = await self.db.collection("turns").document(turn_id).get()
        return snap.to_dict() if snap.exists else None

    async def mark_turn(self, turn_id, report_id):
        await (
            self.db.collection("turns")
            .document(turn_id)
            .set(
                {"invalidated_report_id": report_id, "invalidated_at": datetime.now(UTC)},
                merge=True,
            )
        )

    async def add_eval_candidate(self, report_id, doc):
        await self.db.collection("eval_candidates").document(report_id).set(doc)

    async def audit(self, doc):
        await self.db.collection("admin_audit").document(str(uuid.uuid4())).set(doc)

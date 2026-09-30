"""제보·꿀팁·투표 저장소. 운영은 Firestore, 테스트는 메모리.

컬렉션
- reports/{id}: type=tip|wrong_info, status, text_masked(원문 저장 안 함), receipt_hash …
- report_votes/{tip_id}_{token_hash}: 꿀팁 한 개에 투표 토큰당 1표(값 변경만 가능)
- report_quota/{day}_{key}: 하루 한도 카운터(트랜잭션)
- report_texts/{text_hash}: 중복 본문 차단
- report_config/secret: 네트워크 HMAC 비밀값(서버만 읽음)
- eval_candidates/{report_id}: 확인된 오류 제보의 평가 후보(관리자 확인 후 평가셋으로)
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

from backend.reports.rules import evaluate_tip


class ReportStore(Protocol):
    async def secret(self) -> bytes: ...
    async def take_quota(self, key: str, limit: int, day: str) -> bool: ...
    async def claim_text(self, text_hash: str) -> bool: ...
    async def create(self, doc: dict[str, Any]) -> str: ...
    async def get(self, report_id: str) -> dict[str, Any] | None: ...
    async def update(self, report_id: str, fields: dict[str, Any]) -> None: ...
    async def list(self, *, type_: str, statuses: tuple[str, ...] | None = None) -> list[dict]: ...
    async def vote(
        self, tip_id: str, token: str, net: str, value: str, now: datetime
    ) -> dict[str, Any] | None: ...
    async def my_votes(self, token: str, tip_ids: list[str]) -> dict[str, str]: ...
    async def turn(self, turn_id: str) -> dict[str, Any] | None: ...
    async def mark_turn(self, turn_id: str, report_id: str) -> None: ...
    async def add_eval_candidate(self, report_id: str, doc: dict[str, Any]) -> None: ...
    async def audit(self, doc: dict[str, Any]) -> None: ...


def _apply_vote(tip: dict[str, Any], prev: str | None, value: str, net: str) -> dict[str, Any]:
    """투표 집계 변경분. 같은 값 재투표는 변화 없음, 값 변경은 이전 표를 빼고 더한다."""
    counts = {k: int(tip.get(k, 0)) for k in ("confirm", "dispute", "flags")}
    nets = dict(tip.get("net_counts") or {})
    field = {"confirm": "confirm", "dispute": "dispute", "flag": "flags"}
    if prev == value:
        return {}
    if prev in field:
        counts[field[prev]] = max(0, counts[field[prev]] - 1)
        if prev in ("confirm", "dispute"):
            nets[net] = max(0, nets.get(net, 1) - 1)
    counts[field[value]] += 1
    if value in ("confirm", "dispute"):
        nets[net] = nets.get(net, 0) + 1
    out: dict[str, Any] = {**counts, "net_counts": {k: v for k, v in nets.items() if v}}
    if value == "confirm":
        out["last_confirm_at"] = datetime.now(UTC)  # 챗봇 인용 신선도(GPT5 #717-2)
    return out


class MemoryReportStore:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.votes: dict[str, str] = {}
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

    async def vote(self, tip_id, token, net, value, now):
        tip = self.rows.get(tip_id)
        if not tip or tip.get("type") != "tip":
            return None
        key = f"{tip_id}_{token}"
        change = _apply_vote(tip, self.votes.get(key), value, net)
        self.votes[key] = value
        tip.update(change)
        tip.update(evaluate_tip(tip, now))
        return {"id": tip_id, **tip}

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
            ref = self.db.collection("report_config").document("secret")
            snap = await ref.get()
            value = (snap.to_dict() or {}).get("hmac") if snap.exists else None
            if not value:
                value = secrets.token_hex(32)
                await ref.create({"hmac": value, "created_at": datetime.now(UTC)})
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
            tx.set(ref, {"n": n + 1, "day": day, "updated_at": datetime.now(UTC)})
            return True

        return await txn(self.db.transaction())

    async def claim_text(self, text_hash: str) -> bool:
        from google.api_core.exceptions import AlreadyExists

        try:
            await (
                self.db.collection("report_texts")
                .document(text_hash)
                .create({"at": datetime.now(UTC)})
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

    async def vote(self, tip_id, token, net, value, now):
        tip_ref = self.db.collection("reports").document(tip_id)
        vote_ref = self.db.collection("report_votes").document(f"{tip_id}_{token}")

        @self._fs.async_transactional
        async def txn(tx):
            tip_snap = await tip_ref.get(transaction=tx)
            if not tip_snap.exists or (tip_snap.to_dict() or {}).get("type") != "tip":
                return None
            tip = tip_snap.to_dict() or {}
            vote_snap = await vote_ref.get(transaction=tx)
            prev = (vote_snap.to_dict() or {}).get("value") if vote_snap.exists else None
            change = _apply_vote(tip, prev, value, net)
            tip.update(change)
            change.update(evaluate_tip(tip, now))
            tip.update(change)
            if change:
                tx.set(tip_ref, {**change, "updated_at": now}, merge=True)
            tx.set(vote_ref, {"value": value, "tip_id": tip_id, "net": net, "at": now})
            return {"id": tip_id, **tip}

        return await txn(self.db.transaction())

    async def my_votes(self, token, tip_ids):
        out: dict[str, str] = {}
        refs = [self.db.collection("report_votes").document(f"{t}_{token}") for t in tip_ids]
        async for snap in self.db.get_all(refs):
            if snap.exists:
                d = snap.to_dict() or {}
                out[d.get("tip_id", "")] = d.get("value", "")
        return out

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

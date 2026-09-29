from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.admin.auth import AdminActor
from backend.admin.store import FirestoreAdminStore
from backend.domain import AppError


class Snapshot:
    def __init__(self, ref, value):
        self.id = ref.document_id
        self.exists = value is not None
        self._value = value

    def to_dict(self):
        return dict(self._value) if self._value is not None else None


class DocumentRef:
    def __init__(self, db, collection: str, document_id: str):
        self.db = db
        self.collection = collection
        self.document_id = document_id

    async def get(self, transaction=None):
        del transaction
        return Snapshot(self, self.db.data.get((self.collection, self.document_id)))


class CollectionRef:
    def __init__(self, db, name: str):
        self.db = db
        self.name = name

    def document(self, document_id: str):
        return DocumentRef(self.db, self.name, document_id)


class Transaction:
    def __init__(self, db):
        self.db = db

    def set(self, ref, value, merge=False):
        key = (ref.collection, ref.document_id)
        current = self.db.data.get(key, {}) if merge else {}
        self.db.data[key] = {**current, **value}


class FakeDB:
    def __init__(self):
        self.data = {}

    def collection(self, name: str):
        return CollectionRef(self, name)

    def transaction(self):
        return Transaction(self)


def transactional(fn):
    async def run(tx):
        return await fn(tx)

    return run


def store() -> FirestoreAdminStore:
    value = FirestoreAdminStore.__new__(FirestoreAdminStore)
    value._fs = SimpleNamespace(async_transactional=transactional)
    value.db = FakeDB()
    return value


@pytest.mark.asyncio
async def test_add_and_remove_admin_are_idempotent_and_audited() -> None:
    target = store()
    actor = AdminActor(email="bootstrap@example.edu", subject="sub")

    added, reused = await target.add_admin_user(
        "new@example.edu",
        note="운영 담당",
        actor=actor,
        reason="담당 추가",
        request_id="request-add-1",
    )
    replay, replayed = await target.add_admin_user(
        "new@example.edu",
        note="운영 담당",
        actor=actor,
        reason="담당 추가",
        request_id="request-add-1",
    )

    assert reused is False and replayed is True
    assert added["status"] == replay["status"] == "active"
    audits = [row for (collection, _), row in target.db.data.items() if collection == "admin_audit"]
    assert len(audits) == 1
    assert audits[0]["action"] == "admin.add"
    assert audits[0]["reason"] == "담당 추가"

    removed, reused = await target.remove_admin_user(
        "new@example.edu",
        actor=actor,
        reason="담당 변경",
        request_id="request-remove-1",
    )
    replay, replayed = await target.remove_admin_user(
        "new@example.edu",
        actor=actor,
        reason="담당 변경",
        request_id="request-remove-1",
    )

    assert reused is False and replayed is True
    assert removed["status"] == replay["status"] == "removed"
    audits = [row for (collection, _), row in target.db.data.items() if collection == "admin_audit"]
    assert len(audits) == 2
    assert {row["action"] for row in audits} == {"admin.add", "admin.remove"}


@pytest.mark.asyncio
async def test_request_id_cannot_be_reused_for_another_admin_change() -> None:
    target = store()
    actor = AdminActor(email="bootstrap@example.edu", subject="sub")
    await target.add_admin_user(
        "first@example.edu",
        note="",
        actor=actor,
        reason="추가",
        request_id="same-request",
    )

    with pytest.raises(AppError) as caught:
        await target.add_admin_user(
            "second@example.edu",
            note="",
            actor=actor,
            reason="추가",
            request_id="same-request",
        )

    assert caught.value.code == "BAD_REQUEST"

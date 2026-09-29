from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from backend.app.config import Settings
from backend.tools.knowledge import search_academic_knowledge


@dataclass
class _Document:
    id: str
    struct_data: dict[str, Any]


@dataclass
class _Result:
    document: _Document


class _SearchClient:
    def __init__(self, rows_by_kind: dict[str, list[tuple[str, dict[str, Any]]]]) -> None:
        self.rows_by_kind = rows_by_kind
        self.requests = []

    def search(self, *, request, timeout):
        self.requests.append((request, timeout))
        kind = "guide" if '"guide"' in request.filter else "rule"
        return [
            _Result(_Document(document_id, data))
            for document_id, data in self.rows_by_kind[kind]
        ]


class _Snapshot:
    def __init__(self, document_id: str, exists: bool) -> None:
        self.id = document_id
        self.exists = exists


class _DocumentRef:
    def __init__(self, document_id: str) -> None:
        self.id = document_id


class _Collection:
    def document(self, document_id: str) -> _DocumentRef:
        return _DocumentRef(document_id)


class _Firestore:
    def __init__(self, disabled: set[str] | None = None, *, fail: bool = False) -> None:
        self.disabled = disabled or set()
        self.fail = fail

    def collection(self, name: str) -> _Collection:
        assert name == "disabled_document_ids"
        return _Collection()

    def get_all(self, refs):
        if self.fail:
            raise RuntimeError("firestore unavailable")
        return [_Snapshot(ref.id, ref.id in self.disabled) for ref in refs]


def _settings() -> Settings:
    return Settings(gcp_project_id="test-project", search_datastore_id="academic")


@pytest.mark.asyncio
async def test_search_fans_out_merges_and_applies_denylist() -> None:
    client = _SearchClient(
        {
            "rule": [
                (
                    "rule-1",
                    {
                        "source_kind": "rule",
                        "article_title": "제1조",
                        "body": "공개 규정",
                        "source_url": "https://yz.kyungnam.ac.kr/rule/",
                    },
                ),
                ("disabled", {"source_kind": "rule", "body": "회수된 규정"}),
            ],
            "guide": [
                (
                    "guide-1",
                    {
                        "source_kind": "guide",
                        "title": "휴학 안내",
                        "body": "휴학 절차",
                        "source_url": "https://www.kyungnam.ac.kr/ko/4401/subview.do",
                    },
                )
            ],
        }
    )

    result = await search_academic_knowledge(
        "휴학",
        ["rule", "guide"],
        top_k=3,
        settings=_settings(),
        search_client_factory=lambda: client,
        firestore_client_factory=lambda: _Firestore({"disabled"}),
    )

    assert result.ok
    assert [item.id for item in result.items] == ["rule-1", "guide-1"]
    assert [item.kind for item in result.items] == ["article", "guide"]
    assert result.missing_source_kinds == []
    assert len(client.requests) == 2
    assert all('access: ANY("public")' in request.filter for request, _ in client.requests)


@pytest.mark.asyncio
async def test_search_reports_empty_requested_kind_as_partial_result() -> None:
    client = _SearchClient(
        {
            "rule": [("rule-1", {"source_kind": "rule", "body": "규정"})],
            "guide": [],
        }
    )
    result = await search_academic_knowledge(
        "휴학",
        ["rule", "guide"],
        settings=_settings(),
        search_client_factory=lambda: client,
        firestore_client_factory=_Firestore,
    )

    assert result.ok
    assert result.missing_source_kinds == ["guide"]
    assert result.message


@pytest.mark.asyncio
async def test_search_fails_closed_when_denylist_is_unavailable() -> None:
    client = _SearchClient(
        {"rule": [("rule-1", {"source_kind": "rule", "body": "규정"})], "guide": []}
    )
    result = await search_academic_knowledge(
        "휴학",
        ["rule"],
        settings=_settings(),
        search_client_factory=lambda: client,
        firestore_client_factory=lambda: _Firestore(fail=True),
    )

    assert not result.ok
    assert result.error_code == "UPSTREAM_ERROR"
    assert result.items == []


@pytest.mark.asyncio
@pytest.mark.parametrize("query,kinds", [("", ["rule"]), ("휴학", []), ("휴학", ["notice"])])
async def test_search_rejects_bad_input(query, kinds) -> None:
    result = await search_academic_knowledge(query, kinds, settings=_settings())
    assert not result.ok
    assert result.error_code == "BAD_INPUT"

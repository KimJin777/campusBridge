"""Vertex AI Search backed academic knowledge tool."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Sequence
from itertools import islice
from typing import Any, Literal

from google.api_core.client_options import ClientOptions
from google.cloud import discoveryengine_v1 as discoveryengine
from google.cloud import firestore

from backend.app.config import Settings
from backend.domain import Evidence, ToolResult
from backend.tools.common import plain_mapping, truncate, utc_now

SourceKind = Literal["rule", "guide"]
SearchFactory = Callable[[], Any]
FirestoreFactory = Callable[[], Any]
SearchRows = list[tuple[str, dict[str, Any]]]
SearchOutcome = tuple[SourceKind, SearchRows, bool]


def _serving_config(settings: Settings) -> str:
    configured = settings.search_serving_config.strip()
    if configured:
        if configured.startswith("projects/") and "/servingConfigs/" in configured:
            return configured
        engine_id = configured.removesuffix("/servingConfigs/default_search").strip("/")
    else:
        engine_id = f"{settings.search_datastore_id}-search"
    return (
        f"projects/{settings.gcp_project_id}/locations/{settings.search_location}/"
        f"collections/default_collection/engines/{engine_id}/servingConfigs/default_search"
    )


def _search_factory(settings: Settings) -> Any:
    endpoint = f"{settings.search_location}-discoveryengine.googleapis.com"
    return discoveryengine.SearchServiceClient(client_options=ClientOptions(api_endpoint=endpoint))


def _firestore_factory(settings: Settings) -> Any:
    return firestore.Client(project=settings.gcp_project_id, database=settings.firestore_db)


def _run_search(
    *,
    client: Any,
    serving_config: str,
    query: str,
    kind: SourceKind,
    page_size: int,
) -> SearchRows:
    request = discoveryengine.SearchRequest(
        serving_config=serving_config,
        query=query,
        filter=f'access: ANY("public") AND source_kind: ANY("{kind}")',
        page_size=page_size,
    )
    pager = client.search(request=request, timeout=5.0)
    return [
        (result.document.id, plain_mapping(result.document.struct_data))
        for result in islice(pager, page_size)
    ]


def _load_disabled_ids(client: Any, document_ids: Sequence[str]) -> set[str]:
    if not document_ids:
        return set()
    collection = client.collection("disabled_document_ids")
    refs = [collection.document(document_id) for document_id in document_ids]
    disabled: set[str] = set()
    for snapshot in client.get_all(refs):
        if snapshot.exists:
            disabled.add(snapshot.id)
    return disabled


def _denylist_keys(document_id: str, data: dict[str, Any]) -> set[str]:
    """Return every denylist key that can identify a search result.

    New upload chunks carry ``parent_document_id``.  Older indexed chunks do
    not, so retain compatibility with the stable ``doc:{parent}:{chunk}``
    evidence-id format.
    """
    article_id = str(data.get("article_id") or document_id)
    keys = {document_id, article_id}
    parent = str(data.get("parent_document_id") or "").strip()
    if not parent and article_id.startswith("doc:"):
        parent, separator, chunk = article_id.removeprefix("doc:").rpartition(":")
        if not separator or not parent or not chunk.isdigit():
            parent = ""
    if parent:
        keys.add(parent)
    return keys


def _int_like(value: Any) -> Any:
    """검색 structData 숫자는 float로 온다(66.0) — 정수면 정수로 표기."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _rule_title(data: dict[str, Any]) -> str:
    """근거 카드 제목: "경남대학교 학칙 제38조(휴학)" — 부칙은 "… 부칙"."""
    rule = str(data.get("rule_name") or "규정")
    if data.get("kind") == "addenda":
        return f"{rule} 부칙"
    no = _int_like(data.get("article_no"))
    if (
        data.get("kind") == "appendix"
    ):  # 별표(교수님 2026-10-01 #883): "… 학칙 [별표 2] 학사학위종별표"
        sub = _int_like(data.get("article_branch"))
        label = f"[별표 {no}{f'-{sub}' if sub else ''}]"
        return f"{rule} {label} {data.get('article_title') or ''}".strip()
    if no is None:
        return str(data.get("article_title") or rule)
    branch_no = _int_like(data.get("article_branch"))
    branch = f"의{branch_no}" if branch_no else ""
    title = f"({data['article_title']})" if data.get("article_title") else ""
    return f"{rule} 제{no}조{branch}{title}"


def _as_evidence(document_id: str, data: dict[str, Any], kind: SourceKind) -> Evidence:
    if kind == "guide":
        title = str(data.get("title") or data.get("article_title") or "학사안내")
        evidence_kind = "guide"
    else:
        title = _rule_title(data)
        evidence_kind = "article"
    body = str(data.get("body") or "").strip()
    meta_keys = (
        "source_kind",
        "department",
        "dept_id",
        "revision_date",
        "revision_no",
        "rule_level",
        "has_table",
        "delegated",
        "page_modified",
        "fetched_at",
    )
    meta = {key: data[key] for key in meta_keys if data.get(key) is not None}
    return Evidence(
        # Vertex ID는 [A-Za-z0-9_-]만 허용 → 설계 근거 ID(guide:leave:2 등)는 article_id
        id=str(data.get("article_id") or document_id),
        kind=evidence_kind,
        title=title,
        text=body,
        url=str(data.get("source_url") or "") or None,
        meta=meta,
    )


def _merge_results(
    results: Iterable[SearchOutcome],
    *,
    disabled_ids: set[str],
    top_k: int,
) -> list[Evidence]:
    per_kind: dict[SourceKind, list[Evidence]] = {}
    for kind, rows, _ in results:
        per_kind[kind] = [
            _as_evidence(document_id, data, kind)
            for document_id, data in rows
            if not (_denylist_keys(document_id, data) & disabled_ids)
            and str(data.get("body") or "").strip()
        ]

    merged: list[Evidence] = []
    seen: set[str] = set()
    # A round-robin merge prevents one source kind from consuming the global cap.
    depth = 0
    while len(merged) < top_k and any(depth < len(rows) for rows in per_kind.values()):
        for rows in per_kind.values():
            if depth < len(rows) and rows[depth].id not in seen:
                merged.append(rows[depth])
                seen.add(rows[depth].id)
                if len(merged) == top_k:
                    break
        depth += 1
    return merged


async def search_academic_knowledge(
    query: str,
    kinds: list[SourceKind],
    original_query: str | None = None,
    top_k: int = 5,
    *,
    settings: Settings,
    search_client_factory: SearchFactory | None = None,
    firestore_client_factory: FirestoreFactory | None = None,
) -> ToolResult:
    """Find exact source text from public rules and academic guides."""

    normalized_query = truncate(query, 100)
    fallback_query = truncate(original_query, 100)
    normalized_kinds = list(dict.fromkeys(kinds))
    if (
        not normalized_query
        or not normalized_kinds
        or any(kind not in {"rule", "guide"} for kind in normalized_kinds)
    ):
        return ToolResult.fail("BAD_INPUT", "검색어와 검색 종류를 확인해 주세요")
    if not settings.gcp_project_id or not settings.search_datastore_id:
        return ToolResult.fail("UPSTREAM_ERROR", "검색 서비스 설정이 없습니다")
    top_k = max(1, min(int(top_k), 8))
    factory = search_client_factory or (lambda: _search_factory(settings))

    async def search_one(
        kind: SourceKind,
    ) -> SearchOutcome:
        rows = await asyncio.to_thread(
            _run_search,
            client=factory(),
            serving_config=_serving_config(settings),
            query=normalized_query,
            kind=kind,
            page_size=top_k,
        )
        used_fallback = False
        if not rows and fallback_query and fallback_query != normalized_query:
            rows = await asyncio.to_thread(
                _run_search,
                client=factory(),
                serving_config=_serving_config(settings),
                query=fallback_query,
                kind=kind,
                page_size=top_k,
            )
            used_fallback = True
        return kind, rows, used_fallback

    tasks = [asyncio.create_task(search_one(kind)) for kind in normalized_kinds]
    done = await asyncio.gather(*tasks, return_exceptions=True)
    successful = [row for row in done if not isinstance(row, BaseException)]
    failed_kinds = [
        kind
        for kind, row in zip(normalized_kinds, done, strict=True)
        if isinstance(row, BaseException)
    ]
    if not successful:
        timeout = any(isinstance(row, TimeoutError) for row in done)
        return ToolResult.fail(
            "UPSTREAM_TIMEOUT" if timeout else "UPSTREAM_ERROR",
            "학사 근거 검색을 완료하지 못했습니다",
        )

    document_ids = [
        ident
        for _, rows, _ in successful
        for document_id, data in rows
        for ident in _denylist_keys(document_id, data)
    ]
    try:
        fs_factory = firestore_client_factory or (lambda: _firestore_factory(settings))
        disabled_ids = await asyncio.to_thread(_load_disabled_ids, fs_factory(), document_ids)
    except Exception:
        # The emergency withdrawal list is fail-closed: never leak a potentially disabled item.
        return ToolResult.fail("UPSTREAM_ERROR", "근거 공개 상태를 확인하지 못했습니다")

    items = _merge_results(successful, disabled_ids=disabled_ids, top_k=top_k)
    fallback_used = any(used for _, _, used in successful)
    missing = [
        kind
        for kind in normalized_kinds
        if kind in failed_kinds or not any(item.meta.get("source_kind") == kind for item in items)
    ]
    return ToolResult(
        ok=True,
        items=items,
        message=(
            "일부 출처에서 근거를 찾지 못했습니다"
            if missing
            else "원문 질의로 다시 검색했습니다"
            if fallback_used
            else None
        ),
        as_of=utc_now(),
        missing_source_kinds=missing,
    )

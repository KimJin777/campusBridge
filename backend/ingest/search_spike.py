"""Run the D1 structured-document spike against Vertex AI Search."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import time
from collections.abc import Sequence
from itertools import islice
from pathlib import Path

from google.api_core.client_options import ClientOptions
from google.api_core.exceptions import NotFound
from google.cloud import discoveryengine_v1 as discoveryengine
from google.cloud import storage

from backend.ingest.manifest import load_manifest, save_manifest

DEFAULT_QUERIES = (
    ("학사경고 기준이 어떻게 되나요?", "29_main_32"),
    ("휴학은 언제까지 신청해야 하나요?", "29_main_38"),
    ("복학하려면 무엇을 해야 하나요?", "29_main_39"),
    ("수업이 휴강되면 보강은 어떻게 하나요?", "316_main_14"),
    ("장학금을 신청할 때 제출할 서류가 있나요?", "156_main_6"),
)
INDEX_VERSION = "preflight-b-v1"


def build_search_schema() -> dict[str, object]:
    searchable = {"type": "string", "searchable": True, "retrievable": True}
    retrievable = {"type": "string", "retrievable": True}
    indexable = {"type": "string", "indexable": True, "retrievable": True}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "dynamic": "false",
        "datetime_detection": True,
        "properties": {
            "article_id": indexable,
            "rule_no": indexable,
            "rule_name": searchable,
            "rule_level": indexable,
            "article_no": {"type": "integer", "retrievable": True},
            "article_branch": {"type": "integer", "retrievable": True},
            "article_title": {
                "type": "string",
                "keyPropertyMapping": "title",
                "retrievable": True,
            },
            "chapter": searchable,
            "section": searchable,
            "kind": indexable,
            "addenda_date": {"type": "datetime", "indexable": True, "retrievable": True},
            "department": searchable,
            "dept_id": indexable,
            "revision_date": {
                "type": "datetime",
                "indexable": True,
                "retrievable": True,
            },
            "revision_no": {"type": "integer", "retrievable": True},
            "source_url": {
                "type": "string",
                "keyPropertyMapping": "uri",
                "retrievable": True,
            },
            "source_kind": indexable,
            "has_table": {"type": "boolean", "indexable": True, "retrievable": True},
            "access": indexable,
            "content_hash": retrievable,
            "body": {
                "type": "string",
                "keyPropertyMapping": "description",
                "retrievable": True,
            },
            "preflight_revision": retrievable,
        },
    }


def _clients() -> tuple[
    discoveryengine.DataStoreServiceClient,
    discoveryengine.SchemaServiceClient,
    discoveryengine.EngineServiceClient,
    discoveryengine.DocumentServiceClient,
    discoveryengine.SearchServiceClient,
]:
    options = ClientOptions(api_endpoint="global-discoveryengine.googleapis.com")
    return (
        discoveryengine.DataStoreServiceClient(client_options=options),
        discoveryengine.SchemaServiceClient(client_options=options),
        discoveryengine.EngineServiceClient(client_options=options),
        discoveryengine.DocumentServiceClient(client_options=options),
        discoveryengine.SearchServiceClient(client_options=options),
    )


def ensure_search_resources(
    *,
    project: str,
    data_store_id: str,
    engine_id: str,
) -> tuple[str, str]:
    data_store_client, schema_client, engine_client, _, _ = _clients()
    parent = f"projects/{project}/locations/global/collections/default_collection"
    data_store_name = f"{parent}/dataStores/{data_store_id}"
    try:
        data_store_client.get_data_store(name=data_store_name)
    except NotFound:
        operation = data_store_client.create_data_store(
            parent=parent,
            data_store_id=data_store_id,
            data_store=discoveryengine.DataStore(
                display_name="rules-articles",
                industry_vertical=discoveryengine.IndustryVertical.GENERIC,
                solution_types=[discoveryengine.SolutionType.SOLUTION_TYPE_SEARCH],
                content_config=discoveryengine.DataStore.ContentConfig.NO_CONTENT,
            ),
        )
        operation.result(timeout=600)

    schema_name = f"{data_store_name}/schemas/default_schema"
    operation = schema_client.update_schema(
        request=discoveryengine.UpdateSchemaRequest(
            schema=discoveryengine.Schema(
                name=schema_name,
                struct_schema=build_search_schema(),
            )
        )
    )
    operation.result(timeout=600)

    engine_name = f"{parent}/engines/{engine_id}"
    try:
        engine_client.get_engine(name=engine_name)
    except NotFound:
        operation = engine_client.create_engine(
            parent=parent,
            engine_id=engine_id,
            engine=discoveryengine.Engine(
                display_name="rules-articles-search",
                data_store_ids=[data_store_id],
                solution_type=discoveryengine.SolutionType.SOLUTION_TYPE_SEARCH,
                industry_vertical=discoveryengine.IndustryVertical.GENERIC,
                search_engine_config=discoveryengine.Engine.SearchEngineConfig(
                    search_tier=discoveryengine.SearchTier.SEARCH_TIER_STANDARD,
                ),
            ),
        )
        operation.result(timeout=600)
    return data_store_name, engine_name


def _upload_jsonl(*, project: str, bucket: str, source: Path, object_name: str) -> str:
    client = storage.Client(project=project)
    blob = client.bucket(bucket).blob(object_name)
    blob.upload_from_filename(source, content_type="application/x-ndjson")
    return f"gs://{bucket}/{object_name}"


def _import_documents(
    *,
    data_store_name: str,
    gcs_uri: str,
) -> tuple[str, float, list[str]]:
    _, _, _, document_client, _ = _clients()
    parent = f"{data_store_name}/branches/default_branch"
    operation = document_client.import_documents(
        request=discoveryengine.ImportDocumentsRequest(
            parent=parent,
            gcs_source=discoveryengine.GcsSource(
                input_uris=[gcs_uri],
                data_schema="document",
            ),
            reconciliation_mode=(
                discoveryengine.ImportDocumentsRequest.ReconciliationMode.FULL
            ),
        )
    )
    operation_name = operation.operation.name
    started = time.perf_counter()
    response = operation.result(timeout=900)
    elapsed = time.perf_counter() - started
    errors = [sample.error_message for sample in response.error_samples]
    return operation_name, elapsed, errors


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _write_overwrite_probe(source: Path, output: Path, target_id: str) -> None:
    documents = _read_jsonl(source)
    matched = False
    for document in documents:
        if document["id"] == target_id:
            document["structData"]["preflight_revision"] = "overwrite-v2"
            matched = True
    if not matched:
        raise ValueError(f"overwrite probe target missing: {target_id}")
    output.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in documents),
        encoding="utf-8",
        newline="\n",
    )


def _get_document(data_store_name: str, document_id: str) -> dict[str, object]:
    _, _, _, document_client, _ = _clients()
    name = f"{data_store_name}/branches/default_branch/documents/{document_id}"
    document = document_client.get_document(name=name)
    return dict(document.struct_data)


def _search(
    *,
    engine_name: str,
    query: str,
    filter_expression: str,
) -> list[tuple[str, dict[str, object]]]:
    _, _, _, _, search_client = _clients()
    serving_config = f"{engine_name}/servingConfigs/default_search"
    pager = search_client.search(
        request=discoveryengine.SearchRequest(
            serving_config=serving_config,
            query=query,
            filter=filter_expression,
            page_size=5,
        )
    )
    return [
        (result.document.id, dict(result.document.struct_data))
        for result in islice(pager, 5)
    ]


def _wait_for_search(engine_name: str, expected_id: str, *, timeout: float = 600) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        results = _search(
            engine_name=engine_name,
            query="휴학",
            filter_expression='access: ANY("public")',
        )
        if any(document_id == expected_id for document_id, _ in results):
            return
        time.sleep(10)
    raise TimeoutError("search index did not expose the imported preflight documents")


def run_spike(
    *,
    project: str,
    bucket: str,
    data_store_id: str,
    engine_id: str,
    source: Path,
    report_path: Path,
    manifest_path: Path,
) -> dict[str, object]:
    documents = _read_jsonl(source)
    local_by_id = {str(item["id"]): item["structData"] for item in documents}
    data_store_name, engine_name = ensure_search_resources(
        project=project,
        data_store_id=data_store_id,
        engine_id=engine_id,
    )
    first_uri = _upload_jsonl(
        project=project,
        bucket=bucket,
        source=source,
        object_name=f"preflight/{INDEX_VERSION}/articles.jsonl",
    )
    first_job, first_seconds, first_errors = _import_documents(
        data_store_name=data_store_name,
        gcs_uri=first_uri,
    )
    if first_errors:
        raise RuntimeError(f"initial import failed: {first_errors}")
    _wait_for_search(engine_name, "29_main_38")

    query_results: list[dict[str, object]] = []
    recall_hits = 0
    exact_bodies = True
    metadata_restored = True
    for query, expected_id in DEFAULT_QUERIES:
        results = _search(
            engine_name=engine_name,
            query=query,
            filter_expression='access: ANY("public")',
        )
        ids = [document_id for document_id, _ in results]
        recall_hits += expected_id in ids
        exact_bodies = exact_bodies and bool(results) and all(
            document_id in local_by_id
            and data.get("body") == local_by_id[document_id].get("body")
            for document_id, data in results
        )
        metadata_restored = metadata_restored and all(
            all(field in data for field in ("body", "source_url", "department", "revision_date"))
            for _, data in results
        )
        query_results.append({"query": query, "expected_id": expected_id, "top5": ids})

    private_results = _search(
        engine_name=engine_name,
        query="휴학",
        filter_expression='access: ANY("private")',
    )
    public_filter = not private_results

    table_preserved = all(
        _get_document(data_store_name, article_id).get("has_table") is True
        for article_id in ("29_main_28", "316_main_9")
    )
    addenda_preserved = all(
        _get_document(data_store_name, article_id).get("kind") == "addenda"
        and "addenda_date" in _get_document(data_store_name, article_id)
        for article_id in ("29_add_s116_0", "316_add_s07_0", "155_add_s14_0", "156_add_s34_0")
    )

    probe_path = report_path.with_suffix(".overwrite-probe.jsonl")
    try:
        _write_overwrite_probe(source, probe_path, "29_main_38")
        second_uri = _upload_jsonl(
            project=project,
            bucket=bucket,
            source=probe_path,
            object_name=f"preflight/{INDEX_VERSION}/articles-overwrite-v2.jsonl",
        )
        second_job, second_seconds, second_errors = _import_documents(
            data_store_name=data_store_name,
            gcs_uri=second_uri,
        )
    finally:
        probe_path.unlink(missing_ok=True)
    if second_errors:
        raise RuntimeError(f"overwrite import failed: {second_errors}")
    overwrite_ok = (
        _get_document(data_store_name, "29_main_38").get("preflight_revision")
        == "overwrite-v2"
    )

    criteria = {
        "1_import_succeeded": True,
        "2_public_filter": public_filter,
        "3_recall_at_5": recall_hits >= 4,
        "4_metadata_restored": metadata_restored,
        "5_same_id_overwrite": overwrite_ok,
        "6_exact_citation_body": exact_bodies,
        "7_table_addenda_preserved": table_preserved and addenda_preserved,
        "8_reindex_time_recorded": second_seconds > 0,
    }
    report: dict[str, object] = {
        "created_at": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        "project": project,
        "data_store_id": data_store_id,
        "engine_id": engine_id,
        "index_version": INDEX_VERSION,
        "document_count": len(documents),
        "initial_import": {
            "operation": first_job,
            "gcs_uri": first_uri,
            "elapsed_seconds": round(first_seconds, 3),
        },
        "overwrite_import": {
            "operation": second_job,
            "gcs_uri": second_uri,
            "elapsed_seconds": round(second_seconds, 3),
        },
        "recall_at_5": {"hits": recall_hits, "total": len(DEFAULT_QUERIES)},
        "queries": query_results,
        "criteria": criteria,
        "passed": all(criteria.values()),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    if report["passed"]:
        entries = load_manifest(manifest_path)
        for rule_no, entry in entries.items():
            entries[rule_no] = dataclasses.replace(
                entry,
                status="indexed",
                index_version=INDEX_VERSION,
                import_job_id=second_job,
            )
        save_manifest(manifest_path, entries)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Vertex AI Search D1 format-B spike")
    parser.add_argument("--project", default="campusbridge-510101")
    parser.add_argument("--bucket", default="campusbridge-510101-rules")
    parser.add_argument("--data-store-id", default="rules-articles")
    parser.add_argument("--engine-id", default="rules-articles-search")
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("data/processed/preflight_articles.jsonl"),
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("data/processed/preflight_search_report.json"),
    )
    parser.add_argument("--manifest", type=Path, default=Path("data/processed/manifest.json"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_spike(
        project=args.project,
        bucket=args.bucket,
        data_store_id=args.data_store_id,
        engine_id=args.engine_id,
        source=args.source,
        report_path=args.report,
        manifest_path=args.manifest,
    )
    print(
        f"passed={report['passed']} recall={report['recall_at_5']} "
        f"report={args.report}"
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""전체 규정 색인(상세설계 01 §1~§3, D1 안 B).

목록 → 다운로드 → 변환·조 분할·검수 → JSONL → Vertex AI Search 가져오기.

실행:
    uv run python -m backend.ingest.full_index --download --import-index

- 규정 단위로 실패를 격리한다: 변환·검수 실패 규정은 manifest를 rejected로 두고 색인에서 뺀다
  (기존 색인을 지우지 않도록 FULL 대신 INCREMENTAL로 가져온다 — 빠진 규정은 이전 버전 유지).
- 2,000자 초과 조문은 항(①…) 단위로, 없으면 1,500자 단위로 나눈다(01 §1).
- 결과 요약은 data/processed/full_index_report.json에 남긴다.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import json
import os
from collections.abc import Sequence
from pathlib import Path

from backend.ingest.download import download_rules
from backend.ingest.indexing import _write_jsonl, build_structured_document
from backend.ingest.manifest import load_manifest, save_manifest
from backend.ingest.registry import RuleRecord, read_rules
from backend.ingest.rules import chunk_article, convert_hwp, split_articles, validate_articles

INDEX_VERSION = "articles-v1"


def build_documents(
    records: Sequence[RuleRecord],
    *,
    manifest_path: Path,
    raw_dir: Path,
    interim_dir: Path,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """변환·분할·검수 후 색인 문서 목록과 규정별 결과를 돌려준다."""
    entries = load_manifest(manifest_path)
    documents: list[dict[str, object]] = []
    per_rule: dict[str, object] = {}
    seen: set[str] = set()
    for rule in records:
        entry = entries.get(rule.rule_no)
        source = raw_dir / f"{rule.rule_no}_{rule.file_version}.hwp"
        if entry is None or not source.exists():
            per_rule[rule.rule_no] = {"status": "missing_source"}
            continue
        try:
            converted = convert_hwp(source, output_dir=interim_dir, output_stem=rule.rule_no)
            articles = split_articles(converted.text, rule_no=rule.rule_no)
            report = validate_articles(articles, converted_text_length=len(converted.text))
            if not report.can_update_index:
                raise ValueError("; ".join(report.errors) or "validation held index update")
        except Exception as exc:  # noqa: BLE001 — 규정 단위 격리
            entries[rule.rule_no] = dataclasses.replace(entry, status="rejected")
            per_rule[rule.rule_no] = {"status": "rejected", "error": str(exc)[:200]}
            continue
        count = 0
        for article in articles:
            if not article.indexable:
                continue
            for chunk in chunk_article(article):
                doc = build_structured_document(chunk, rule, entry)
                if doc["id"] in seen:
                    continue  # 중복 ID는 첫 항목만(검수 보고에 이미 기록됨)
                seen.add(str(doc["id"]))
                doc["structData"]["index_version"] = INDEX_VERSION  # type: ignore[index]
                documents.append(doc)
                count += 1
        entries[rule.rule_no] = dataclasses.replace(
            entry,
            converter=converted.converter,
            article_count=report.article_count,
            status="validated",
            index_version=INDEX_VERSION,
        )
        per_rule[rule.rule_no] = {
            "status": "validated",
            "rule_name": rule.rule_name,
            "articles": report.article_count,
            "documents": count,
        }
    save_manifest(manifest_path, entries)
    return documents, per_rule


def import_index(
    *,
    project: str,
    bucket: str,
    data_store_id: str,
    engine_id: str,
    source: Path,
    object_prefix: str = "index",
):
    """GCS 업로드 후 INCREMENTAL 가져오기(같은 ID는 덮어쓰기, 빠진 규정은 기존 유지)."""
    from google.cloud import discoveryengine_v1 as discoveryengine

    from backend.ingest.search_spike import _clients, _upload_jsonl, ensure_search_resources

    data_store_name, _ = ensure_search_resources(
        project=project, data_store_id=data_store_id, engine_id=engine_id
    )
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    uri = _upload_jsonl(
        project=project,
        bucket=bucket,
        source=source,
        object_name=f"{object_prefix}/{INDEX_VERSION}/{stamp}/{source.name}",
    )
    _, _, _, document_client, _ = _clients()
    operation = document_client.import_documents(
        request=discoveryengine.ImportDocumentsRequest(
            parent=f"{data_store_name}/branches/default_branch",
            gcs_source=discoveryengine.GcsSource(input_uris=[uri], data_schema="document"),
            reconciliation_mode=discoveryengine.ImportDocumentsRequest.ReconciliationMode.INCREMENTAL,
        )
    )
    response = operation.result(timeout=1800)
    errors = [s.message for s in response.error_samples]
    return {"gcs_uri": uri, "operation": operation.operation.name, "errors": errors[:20]}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Build and import the full regulation index")
    p.add_argument("--rules-file", type=Path, default=Path("data/processed/rules.json"))
    p.add_argument("--manifest", type=Path, default=Path("data/processed/manifest.json"))
    p.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    p.add_argument("--interim-dir", type=Path, default=Path("data/interim"))
    p.add_argument("--output", type=Path, default=Path("data/processed/articles.jsonl"))
    p.add_argument("--report", type=Path, default=Path("data/processed/full_index_report.json"))
    p.add_argument("--rule-nos", nargs="*", help="일부 규정만(기본: 목록 전체)")
    p.add_argument("--download", action="store_true", help="HWP를 먼저 내려받는다(요청 간격 1초)")
    p.add_argument("--import-index", action="store_true", help="Vertex AI Search로 가져온다")
    p.add_argument("--project", default=os.environ.get("GCP_PROJECT_ID", ""))
    p.add_argument("--bucket", default=os.environ.get("RULES_BUCKET", ""))
    p.add_argument(
        "--data-store-id", default=os.environ.get("SEARCH_DATASTORE_ID", "rules-articles")
    )
    p.add_argument(
        "--engine-id", default=os.environ.get("SEARCH_ENGINE_ID", "rules-articles-search")
    )
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    records = read_rules(args.rules_file)
    if args.rule_nos:
        wanted = set(args.rule_nos)
        records = [r for r in records if r.rule_no in wanted]
    if args.download:
        asyncio.run(download_rules(records, output_dir=args.raw_dir, manifest_path=args.manifest))
    documents, per_rule = build_documents(
        records, manifest_path=args.manifest, raw_dir=args.raw_dir, interim_dir=args.interim_dir
    )
    _write_jsonl(args.output, documents)
    statuses = [v["status"] for v in per_rule.values()]  # type: ignore[index]
    report: dict[str, object] = {
        "created_at": dt.datetime.now(dt.UTC).isoformat(),
        "index_version": INDEX_VERSION,
        "rules": len(records),
        "validated": statuses.count("validated"),
        "rejected": statuses.count("rejected"),
        "missing_source": statuses.count("missing_source"),
        "documents": len(documents),
        "tables": sum(1 for d in documents if d["structData"].get("has_table")),  # type: ignore[union-attr]
        "per_rule": per_rule,
    }
    if args.import_index:
        if not args.project or not args.bucket:
            raise SystemExit("--project/--bucket(GCP_PROJECT_ID/RULES_BUCKET)가 필요합니다")
        report["import"] = import_index(
            project=args.project,
            bucket=args.bucket,
            data_store_id=args.data_store_id,
            engine_id=args.engine_id,
            source=args.output,
        )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        k: report[k]
        for k in ("rules", "validated", "rejected", "missing_source", "documents", "tables")
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

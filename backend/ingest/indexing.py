"""Build validated structured JSONL documents for Vertex AI Search."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path

from backend.ingest.manifest import ManifestEntry, load_manifest, save_manifest
from backend.ingest.registry import RuleRecord, read_rules
from backend.ingest.rules import Article, convert_hwp, split_articles, validate_articles

DEFAULT_PREFLIGHT_IDS = (
    "29_main_28",  # 표 토큰 보존
    "29_main_32",  # 학사경고
    "29_main_38",  # 휴학
    "29_main_39",  # 복학
    "29_add_s116_0",  # 최신 부칙
    "316_main_5",  # 수업시간
    "316_main_9",  # 표 토큰 보존
    "316_main_13",  # 출결관리
    "316_main_14",  # 휴·결강과 보강
    "316_add_s07_0",  # 최신 부칙
    "155_main_3",  # 장학금 종류
    "155_main_24",  # 장학금 신청
    "155_main_26",  # 지급제한
    "155_main_27",  # 이중수혜 제한
    "155_add_s14_0",  # 최신 부칙
    "156_main_3",  # 이중수혜 대상
    "156_main_4",  # 성적우수 선발
    "156_main_6",  # 제출서류
    "156_main_8",  # 환수
    "156_add_s34_0",  # 최신 부칙
)


def _iso_date(value: str | None) -> str | None:
    if not value:
        return None
    for pattern in ("%Y.%m.%d", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(value.rstrip("."), pattern).date().isoformat()
        except ValueError:
            pass
    raise ValueError(f"unsupported registry date: {value}")


def build_structured_document(
    article: Article,
    rule: RuleRecord,
    manifest: ManifestEntry,
    *,
    dept_id: str | None = None,
) -> dict[str, object]:
    """Render format B: all searchable text and metadata live in structData."""

    if not article.indexable:
        raise ValueError(f"article is not indexable: {article.article_id}")
    data: dict[str, object] = {
        "article_id": article.article_id,
        "rule_no": rule.rule_no,
        "rule_name": rule.rule_name,
        "rule_level": rule.rule_level,
        "article_no": article.number,
        "article_branch": article.branch,
        "article_title": article.title,
        "chapter": article.chapter,
        "section": article.section,
        "kind": article.mode.value,
        "addenda_date": article.addenda_date.isoformat() if article.addenda_date else None,
        "department": rule.department,
        "revision_date": _iso_date(rule.revision_date),
        "revision_no": rule.revision_no,
        "source_url": rule.hwp_url,
        "source_kind": "rule",
        "has_table": article.has_table,
        "access": "public",
        "content_hash": manifest.content_hash,
        "body": article.body,
    }
    data = {key: value for key, value in data.items() if value is not None}
    if dept_id:
        data["dept_id"] = dept_id
    return {"id": article.article_id, "structData": data}


def _write_jsonl(path: Path, documents: Sequence[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = "".join(
        json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n"
        for document in documents
    )
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def build_preflight_jsonl(
    *,
    records: Sequence[RuleRecord],
    manifest_path: Path,
    raw_dir: Path,
    interim_dir: Path,
    output: Path,
    sample_ids: Sequence[str] = DEFAULT_PREFLIGHT_IDS,
    dept_ids: Mapping[str, str] | None = None,
) -> list[dict[str, object]]:
    """Convert four academic regulations and emit the fixed 20-article D1 sample."""

    if len(sample_ids) != 20 or len(set(sample_ids)) != 20:
        raise ValueError("preflight sample must contain exactly 20 unique article IDs")
    requested_counts = Counter(article_id.split("_", 1)[0] for article_id in sample_ids)
    if set(requested_counts.values()) != {5}:
        raise ValueError("preflight sample must contain exactly five articles per regulation")

    records_by_no = {record.rule_no: record for record in records}
    entries = load_manifest(manifest_path)
    articles_by_id: dict[str, Article] = {}
    for rule_no in requested_counts:
        rule = records_by_no.get(rule_no)
        entry = entries.get(rule_no)
        if rule is None or entry is None:
            raise ValueError(f"registry or manifest entry missing for rule {rule_no}")
        source = raw_dir / f"{rule_no}_{rule.file_version}.hwp"
        try:
            converted = convert_hwp(source, output_dir=interim_dir, output_stem=rule_no)
            articles = split_articles(converted.text, rule_no=rule_no)
            report = validate_articles(articles, converted_text_length=len(converted.text))
            if not report.can_update_index:
                raise ValueError("; ".join(report.errors) or "validation held index update")
        except Exception:
            entries[rule_no] = dataclasses.replace(entry, status="rejected")
            save_manifest(manifest_path, entries)
            raise
        entries[rule_no] = dataclasses.replace(
            entry,
            converter=converted.converter,
            article_count=report.article_count,
            status="validated",
        )
        articles_by_id.update((article.article_id, article) for article in articles)

    missing = [article_id for article_id in sample_ids if article_id not in articles_by_id]
    if missing:
        raise ValueError(f"preflight article IDs missing after parse: {', '.join(missing)}")
    documents = [
        build_structured_document(
            articles_by_id[article_id],
            records_by_no[article_id.split("_", 1)[0]],
            entries[article_id.split("_", 1)[0]],
            dept_id=(dept_ids or {}).get(
                records_by_no[article_id.split("_", 1)[0]].department or ""
            ),
        )
        for article_id in sample_ids
    ]
    _write_jsonl(output, documents)
    save_manifest(manifest_path, entries)
    return documents


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build the 20-article structured search preflight")
    parser.add_argument("--rules-file", type=Path, default=Path("data/processed/rules.json"))
    parser.add_argument("--manifest", type=Path, default=Path("data/processed/manifest.json"))
    parser.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--interim-dir", type=Path, default=Path("data/interim"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/processed/preflight_articles.jsonl"),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    documents = build_preflight_jsonl(
        records=read_rules(args.rules_file),
        manifest_path=args.manifest,
        raw_dir=args.raw_dir,
        interim_dir=args.interim_dir,
        output=args.output,
    )
    print(f"documents={len(documents)} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

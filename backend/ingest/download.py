"""Download allowlisted current HWP files and update the ingestion manifest."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import os
from collections.abc import Iterable, Sequence
from pathlib import Path
from urllib.parse import urlparse

import httpx

from backend.ingest.manifest import ManifestEntry, load_manifest, save_manifest, source_changed
from backend.ingest.registry import ALLOWED_HOST, RuleRecord, read_rules

HWP_OLE_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")


def _user_agent() -> str:
    contact = os.getenv("COLLECTION_CONTACT_EMAIL")
    if not contact:
        raise RuntimeError("COLLECTION_CONTACT_EMAIL is required for identifiable collection")
    return f"CampusBridge/0.1 (contest; contact: {contact})"


def _assert_hwp_url(url: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != ALLOWED_HOST
        or not parsed.path.endswith(".hwp")
    ):
        raise ValueError(f"HWP URL is outside the regulation allowlist: {url}")


def _validate_hwp(content: bytes, content_type: str) -> None:
    if len(content) < 512:
        raise ValueError("downloaded HWP is unexpectedly small")
    if "text/html" in content_type.lower() or content.lstrip().startswith((b"<html", b"<!DOCTYPE")):
        raise ValueError("downloaded HWP endpoint returned HTML")
    if not content.startswith(HWP_OLE_MAGIC):
        raise ValueError("downloaded file does not have the HWP OLE signature")


async def download_rules(
    records: Iterable[RuleRecord],
    *,
    output_dir: Path,
    manifest_path: Path,
    min_interval_seconds: float = 1.0,
    client: httpx.AsyncClient | None = None,
) -> dict[str, ManifestEntry]:
    selected = list(records)
    entries = load_manifest(manifest_path)
    await asyncio.to_thread(output_dir.mkdir, parents=True, exist_ok=True)
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            headers={"User-Agent": _user_agent()},
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
        )
    try:
        for index, record in enumerate(selected):
            _assert_hwp_url(record.hwp_url)
            response = await client.get(record.hwp_url)
            if response.status_code in {403, 429}:
                raise RuntimeError(
                    f"collection stopped: HWP endpoint returned HTTP {response.status_code}"
                )
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            _validate_hwp(response.content, content_type)
            content_hash = f"sha256:{hashlib.sha256(response.content).hexdigest()}"
            destination = output_dir / f"{record.rule_no}_{record.file_version}.hwp"
            previous = entries.get(record.rule_no)
            if source_changed(
                previous,
                file_version=record.file_version,
                content_hash=content_hash,
            ) or not destination.exists():
                partial = destination.with_suffix(".hwp.part")
                partial.write_bytes(response.content)
                partial.replace(destination)
            entries[record.rule_no] = ManifestEntry(
                source_url=record.hwp_url,
                file_version=record.file_version,
                fetched_at=dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
                etag=response.headers.get("etag"),
                last_modified=response.headers.get("last-modified"),
                content_hash=content_hash,
                converter=previous.converter if previous else None,
                article_count=previous.article_count if previous else None,
                status="fetched",
                license_basis="공개 규정",
                index_version=previous.index_version if previous else None,
                import_job_id=previous.import_job_id if previous else None,
            )
            save_manifest(manifest_path, entries)
            if index < len(selected) - 1 and min_interval_seconds > 0:
                await asyncio.sleep(min_interval_seconds)
        return entries
    finally:
        if owns_client:
            await client.aclose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download current allowlisted regulation HWP files"
    )
    parser.add_argument("--rules-file", type=Path, default=Path("data/processed/rules.json"))
    parser.add_argument("--rule-nos", nargs="+", required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--manifest", type=Path, default=Path("data/processed/manifest.json"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    available = {record.rule_no: record for record in read_rules(args.rules_file)}
    missing = sorted(set(args.rule_nos) - set(available))
    if missing:
        raise SystemExit(f"unknown rule numbers: {', '.join(missing)}")
    selected = [available[rule_no] for rule_no in args.rule_nos]
    entries = asyncio.run(
        download_rules(selected, output_dir=args.output_dir, manifest_path=args.manifest)
    )
    print(f"downloaded={len(selected)} manifest_entries={len(entries)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Atomic ingestion manifest updates and change detection."""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Literal

ManifestStatus = Literal["fetched", "converted", "validated", "indexed", "rejected"]


@dataclasses.dataclass(slots=True)
class ManifestEntry:
    source_url: str
    file_version: str
    fetched_at: str
    etag: str | None
    last_modified: str | None
    content_hash: str
    converter: str | None = None
    article_count: int | None = None
    status: ManifestStatus = "fetched"
    license_basis: str = "공개 규정"
    index_version: str | None = None
    import_job_id: str | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ManifestEntry:
        allowed = {field.name for field in dataclasses.fields(cls)}
        return cls(**{key: item for key, item in value.items() if key in allowed})

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)


def load_manifest(path: Path) -> dict[str, ManifestEntry]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("manifest root must be an object")
    return {str(key): ManifestEntry.from_dict(value) for key, value in raw.items()}


def save_manifest(path: Path, entries: dict[str, ManifestEntry]) -> None:
    """Replace the manifest atomically so interrupted runs keep the last good file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {key: entries[key].to_dict() for key in sorted(entries)}
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def source_changed(
    previous: ManifestEntry | None,
    *,
    file_version: str,
    content_hash: str,
) -> bool:
    if previous is None:
        return True
    return previous.file_version != file_version or previous.content_hash != content_hash

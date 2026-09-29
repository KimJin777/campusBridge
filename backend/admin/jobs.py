"""Cloud Run Job launcher for administrator-triggered ingestion."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import quote

import google.auth
from google.auth.transport.requests import AuthorizedSession

from backend.app.config import Settings, get_settings

_RESOURCE_PART = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


class JobLauncher(Protocol):
    async def start(self, run_id: str, source_ids: list[str]) -> str: ...


class CloudRunJobLauncher:
    """Start a configured Cloud Run Job without blocking the API event loop."""

    def __init__(
        self,
        settings: Settings | None = None,
        session_factory: Callable[[Any], Any] = AuthorizedSession,
    ) -> None:
        self.settings = settings or get_settings()
        self._session_factory = session_factory

    def _resource_url(self) -> str:
        project = self.settings.gcp_project_id
        location = self.settings.ingestion_job_location
        job = self.settings.ingestion_job_name
        if not _PROJECT_ID.fullmatch(project):
            raise ValueError("invalid GCP project id")
        if not _RESOURCE_PART.fullmatch(location) or not _RESOURCE_PART.fullmatch(job):
            raise ValueError("invalid Cloud Run Job resource")
        resource = (
            f"projects/{quote(project, safe='')}/locations/{quote(location, safe='')}"
            f"/jobs/{quote(job, safe='')}"
        )
        return f"https://run.googleapis.com/v2/{resource}:run"

    @staticmethod
    def _payload(run_id: str, source_ids: list[str]) -> dict[str, Any]:
        return {
            "overrides": {
                "containerOverrides": [
                    {
                        "env": [
                            {"name": "INGESTION_RUN_ID", "value": run_id},
                            {"name": "SOURCE_IDS", "value": ",".join(source_ids)},
                        ]
                    }
                ]
            }
        }

    def _start_sync(self, run_id: str, source_ids: list[str]) -> str:
        credentials, _ = google.auth.default(scopes=[_CLOUD_PLATFORM_SCOPE])
        session = self._session_factory(credentials)
        response = session.post(
            self._resource_url(),
            json=self._payload(run_id, source_ids),
            timeout=15,
        )
        response.raise_for_status()
        operation = response.json()
        name = operation.get("name")
        if not isinstance(name, str) or not name:
            raise RuntimeError("Cloud Run API returned no operation name")
        return name

    async def start(self, run_id: str, source_ids: list[str]) -> str:
        return await asyncio.to_thread(self._start_sync, run_id, source_ids)


def get_job_launcher() -> JobLauncher:
    return CloudRunJobLauncher()

from __future__ import annotations

import pytest

from backend.admin.jobs import CloudRunJobLauncher
from backend.app.config import Settings


def test_job_launcher_builds_allowlisted_google_resource_and_env() -> None:
    launcher = CloudRunJobLauncher(
        Settings(
            gcp_project_id="campus-bridge1",
            ingestion_job_name="campusbridge-ingest",
            ingestion_job_location="asia-northeast3",
        )
    )

    assert launcher._resource_url() == (
        "https://run.googleapis.com/v2/projects/campus-bridge1/locations/asia-northeast3/"
        "jobs/campusbridge-ingest:run"
    )
    assert launcher._payload({"INGESTION_RUN_ID": "run-1", "SOURCE_IDS": "notices,calendar"})[
        "overrides"
    ]["containerOverrides"][0]["env"] == [
        {"name": "INGESTION_RUN_ID", "value": "run-1"},
        {"name": "SOURCE_IDS", "value": "notices,calendar"},
    ]


def test_job_launcher_rejects_resource_path_injection() -> None:
    launcher = CloudRunJobLauncher(
        Settings(
            gcp_project_id="campus-bridge1",
            ingestion_job_name="../other-job",
        )
    )

    with pytest.raises(ValueError, match="invalid Cloud Run Job resource"):
        launcher._resource_url()

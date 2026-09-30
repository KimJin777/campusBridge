# ruff: noqa: E501
"""운영 관측(#747): 화면 오류 수집·track 계약·실제 IP."""

import re
from pathlib import Path
from typing import get_args

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.schemas import TrackRequest
from backend.app import telemetry
from backend.app.netutil import client_ip_from
from backend.reports.api import get_report_store
from backend.reports.store import MemoryReportStore

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


def client(store):
    app = FastAPI()
    app.include_router(telemetry.router)
    app.dependency_overrides[get_report_store] = lambda: store
    return TestClient(app)


def body(**kw):
    return {"kind": "js_error", "page": "chat", "app_version": "0.22.0", "fp": "abc123", **kw}


def test_client_error_stores_kind_only_with_ttl():
    store = MemoryReportStore()
    r = client(store).post("/api/client-error", json=body())
    assert r.status_code == 204
    (doc,) = store.client_errors
    assert set(doc) == {"kind", "page", "app_version", "fp", "created_at", "expire_at"}
    assert (doc["expire_at"] - doc["created_at"]).days == telemetry.KEEP_DAYS


def test_client_error_rejects_unknown_kind_and_raw_text():
    c = client(MemoryReportStore())
    assert c.post("/api/client-error", json=body(kind="anything")).status_code == 422
    assert (
        c.post("/api/client-error", json=body(message="홍길동 010")).status_code == 204
    )  # 여분 필드는 무시
    store = MemoryReportStore()
    client(store).post("/api/client-error", json=body(fp="Name With Space", app_version="x"))
    assert store.client_errors[0]["fp"] == "" and store.client_errors[0]["app_version"] == ""


def test_client_error_per_network_cap_is_silent(monkeypatch):
    monkeypatch.setattr(telemetry, "PER_NET_DAY", 2)
    store = MemoryReportStore()
    c = client(store)
    codes = [
        c.post("/api/client-error", json=body(), headers={"x-forwarded-for": "8.8.8.8"}).status_code
        for _ in range(4)
    ]
    assert codes == [204] * 4 and len(store.client_errors) == 2
    c.post("/api/client-error", json=body(), headers={"x-forwarded-for": "1.1.1.1"})
    assert len(store.client_errors) == 3


def test_track_contract_frontend_events_are_accepted():
    """화면이 보내는 track 이름이 서버 허용 목록에 모두 있어야 한다(조용히 400으로 버려지던 결함 #746)."""
    sent = set()
    for js in FRONTEND.glob("*.js"):
        sent |= set(re.findall(r'\btrack\("([a-z_]+)"', js.read_text(encoding="utf-8")))
    allowed = set(get_args(TrackRequest.model_fields["event"].annotation))
    assert sent and sent <= allowed, sent - allowed


def test_telemetry_kinds_match_server():
    src = (FRONTEND / "telemetry.js").read_text(encoding="utf-8")
    kinds = set(
        re.findall(r'"([a-z_]+)"', re.search(r"KINDS = new Set\(\[(.*?)\]\)", src).group(1))
    )
    assert kinds == set(get_args(telemetry.ClientError.model_fields["kind"].annotation))


def test_client_ip_ignores_spoofed_first_xff():
    assert client_ip_from("9.9.9.9, 8.8.4.4", "169.254.1.1") == "8.8.4.4"
    assert client_ip_from("8.8.4.4, 35.191.0.1", None) == "8.8.4.4"
    assert client_ip_from(None, "10.0.0.1") == "10.0.0.1"


def test_quality_warning_tiers_exclude_out_of_scope():
    from backend.admin.store import quality_warning

    ok = ["answer"] * 16
    assert quality_warning(ok + ["no_evidence"] * 3)["level"] == "ok"  # 20턴 미만
    assert quality_warning(ok + ["no_evidence"] * 4)["level"] == "warning"  # 4/20 = 20%
    assert quality_warning(ok + ["deadline"] * 9)["level"] == "critical"  # 9/25 = 36%
    assert quality_warning(ok + ["out_of_scope"] * 30)["level"] == "ok"  # 범위 밖은 분모에서도 제외

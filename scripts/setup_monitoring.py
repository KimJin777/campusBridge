# ruff: noqa: E501 — 경보 문구·쿼리는 줄바꿈하지 않는다
"""운영 경보 설정(운영 관측 #747) — 여러 번 실행해도 같은 결과(이름으로 찾아 갱신).

실행(Windows, gcloud 번들 파이썬):
  python scripts/setup_monitoring.py [알림메일]
gcloud auth print-access-token 권한으로 Monitoring·Logging REST API를 부른다.

- 알림: 메일 채널 1개(기본 kjink007@kyungnam.ac.kr)
- 로그 기반 지표: 턴 완료 구조화 로그(main.log_turn)에서 범위 안 턴·답 못 한 턴·시간 초과
- 경보: 답변 품질 위험(2시간 35%↑, 20턴·4건 이상) · 15분 시간 초과 2건↑ ·
  Cloud Run 5xx · 응답 지연 · 매일 수집 Job 실패. 실시간 메일은 위험 등급만(경고는 관리자 화면).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

PROJECT = "campusbridge-510101"
SERVICE = "campusbridge-web"
JOB = "campusbridge-ingest"
EMAIL = sys.argv[1] if len(sys.argv) > 1 else "kjink007@kyungnam.ac.kr"
MON = f"https://monitoring.googleapis.com/v3/projects/{PROJECT}"
LOG = f"https://logging.googleapis.com/v2/projects/{PROJECT}"

TURN = (
    f'resource.type="cloud_run_revision" AND resource.labels.service_name="{SERVICE}" '
    'AND jsonPayload.event="turn_completed"'
)
METRICS = {
    "cb_turns_scoped": ("범위 안 턴(범위 밖 제외)", f'{TURN} AND NOT jsonPayload.fallback_reason="out_of_scope"'),
    "cb_turns_actionable": (
        "답 못 한 턴(근거 없음·검증 탈락·시간 초과)",
        f'{TURN} AND jsonPayload.fallback_reason=("no_evidence" OR "verification_failed" OR "deadline")',
    ),
    "cb_turns_deadline": (
        "시간 초과 턴",
        f'{TURN} AND jsonPayload.fallback_reason=("deadline" OR "timeout")',
    ),
}


def token() -> str:
    import os

    if os.environ.get("GCP_ACCESS_TOKEN"):  # gcloud가 PATH에 없을 때(Windows 번들 설치)
        return os.environ["GCP_ACCESS_TOKEN"]
    gcloud = shutil.which("gcloud") or shutil.which("gcloud.cmd")
    cmd = [gcloud, "auth", "print-access-token"] if gcloud else None
    if cmd is None:
        raise SystemExit("gcloud를 찾을 수 없습니다")
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()


TOKEN = ""


def call(method: str, url: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{method} {url} → {e.code} {e.read().decode()[:500]}") from e


def ensure_channel() -> str:
    for ch in call("GET", f"{MON}/notificationChannels").get("notificationChannels", []):
        if ch.get("type") == "email" and ch.get("labels", {}).get("email_address") == EMAIL:
            return ch["name"]
    ch = call(
        "POST",
        f"{MON}/notificationChannels",
        {"type": "email", "displayName": f"campusBridge 운영 경보 ({EMAIL})", "labels": {"email_address": EMAIL}},
    )
    return ch["name"]


def ensure_metrics() -> None:
    for name, (desc, flt) in METRICS.items():
        body = {
            "name": name,
            "description": desc,
            "filter": flt,
            "metricDescriptor": {"metricKind": "DELTA", "valueType": "INT64", "unit": "1"},
        }
        existing = {m["name"] for m in call("GET", f"{LOG}/metrics").get("metrics", [])}
        if name in existing:
            call("PUT", f"{LOG}/metrics/{name}", body)
        else:
            call("POST", f"{LOG}/metrics", body)


def m(name: str, window: str) -> str:
    return f'sum(increase(logging_googleapis_com:user_{name}{{monitored_resource="cloud_run_revision"}}[{window}]))'


RUN = f'monitored_resource="cloud_run_revision",service_name="{SERVICE}"'
POLICIES = {
    "[campusBridge] 답변 품질 위험": (
        "최근 2시간 범위 안 질문 20건 이상 중 답을 못 한 질문(근거 없음·검증 탈락·시간 초과)이 4건 이상이고 35% 이상입니다. "
        "관리자 화면 > 통계의 '최근 답을 못 한 질문'을 확인하세요.",
        f"({m('cb_turns_actionable', '2h')} / {m('cb_turns_scoped', '2h')} >= 0.35)"
        f" and on() ({m('cb_turns_scoped', '2h')} >= 20) and on() ({m('cb_turns_actionable', '2h')} >= 4)",
    ),
    "[campusBridge] 답변 시간 초과 급증": (
        "15분 안에 답변 시간 초과가 2건 이상입니다. Gemini 지연·장애 가능성을 확인하세요.",
        f"{m('cb_turns_deadline', '15m')} >= 2",
    ),
    "[campusBridge] 서버 오류(5xx)": (
        "5분 안에 서버 오류(5xx) 응답이 5건 이상입니다. Cloud Run 로그를 확인하세요.",
        f'sum(increase(run_googleapis_com:request_count{{{RUN},response_code_class="5xx"}}[5m])) >= 5',
    ),
    "[campusBridge] 응답 지연": (
        "10분간 요청 지연 p95가 30초를 넘었습니다(채팅 스트림 포함).",
        "histogram_quantile(0.95, sum by (le) (rate("
        f"run_googleapis_com:request_latencies_bucket{{{RUN}}}[10m]))) > 30000",
    ),
    "[campusBridge] 매일 수집 Job 실패": (
        "campusbridge-ingest 실행이 실패했습니다. 관리자 화면 > 바로가기 > 수집 Job 실행에서 확인하세요.",
        "sum(increase(run_googleapis_com:job_completed_execution_count"
        f'{{monitored_resource="cloud_run_job",job_name="{JOB}",result="failed"}}[1h])) > 0',
    ),
}


def ensure_policies(channel: str) -> None:
    existing = {
        p["displayName"]: p["name"] for p in call("GET", f"{MON}/alertPolicies").get("alertPolicies", [])
    }
    for title, (doc, query) in POLICIES.items():
        body = {
            "displayName": title,
            "documentation": {"content": doc, "mimeType": "text/markdown"},
            "combiner": "OR",
            "severity": "CRITICAL",
            "conditions": [
                {
                    "displayName": title,
                    "conditionPrometheusQueryLanguage": {
                        "query": query,
                        "duration": "0s",
                        "evaluationInterval": "60s",
                    },
                }
            ],
            "notificationChannels": [channel],
            "alertStrategy": {"autoClose": "21600s"},
        }
        if title in existing:
            call("PATCH", f"https://monitoring.googleapis.com/v3/{existing[title]}", body)
        else:
            call("POST", f"{MON}/alertPolicies", body)
        print("ok", title)


if __name__ == "__main__":
    TOKEN = token()
    ch = ensure_channel()
    print("channel", ch)
    ensure_metrics()
    print("metrics", ", ".join(METRICS))
    ensure_policies(ch)

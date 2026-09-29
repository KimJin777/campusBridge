#!/usr/bin/env bash
# 배포 후 점검(상세설계 07 §7) — 워밍업 겸용. 사용: scripts/smoke.sh <BASE_URL> [APP_VERSION]
# deep=1(관리자 인증) 점검은 점검 전용 인증 경로가 생기기 전까지 401 확인만 한다.
set -euo pipefail
BASE="${1:?base url}"
VER="${2:-}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
uuid() { python3 -c 'import uuid;print(uuid.uuid4())'; }

# 1. status
curl -fsS "$BASE/api/status" -o "$TMP/status.json" || fail "status"
python3 - "$TMP/status.json" "$VER" <<'PY' || fail "status version"
import json,sys
d=json.load(open(sys.argv[1])); v=sys.argv[2]
assert d["status"]=="ok" and d["schema_version"]==1, d
assert not v or d["version"]==v, (d["version"], v)
PY
echo "1 status ok"

# 7. deep=1 without auth → 401
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/status?deep=1")
[ "$code" = "401" ] || fail "deep without auth returned $code"
echo "7 deep auth ok"

chat() { # thread request message out
  python3 -c 'import json,sys;print(json.dumps({"schema_version":1,"thread_id":sys.argv[1],"request_id":sys.argv[2],"message":sys.argv[3]},ensure_ascii=False))' "$1" "$2" "$3" > "$TMP/req.json"
  local start end
  start=$(date +%s)
  curl -fsS -N -X POST "$BASE/api/chat" -H 'Content-Type: application/json; charset=utf-8' --data-binary @"$TMP/req.json" -o "$4" || fail "chat $3"
  end=$(date +%s)
  local took=$((end-start))
  [ "$took" -lt 25 ] || fail "turn took ${took}s (hard deadline 25s)"
  [ "$took" -le 10 ] || echo "WARN: turn took ${took}s (>10s)"
}
outcome() { python3 - "$1" <<'PY'
import json,sys
ev=[b for b in open(sys.argv[1],encoding="utf-8").read().split("\n\n") if b.startswith("event:")]
last=[b for b in ev if b.startswith("event: done")][-1]
print(json.loads(last.split("data: ",1)[1])["outcome"])
PY
}

T=$(uuid); R1=$(uuid)
# 3~4. 휴학 절차는 되묻지 않고 바로 인용 답변(0.9.0, #613 — 이전에는 학년을 되물었음)
Q1="휴학하려면 어떻게 해야 하나요?"
chat "$T" "$R1" "$Q1" "$TMP/t1.txt"
[ "$(outcome "$TMP/t1.txt")" = "answer" ] || fail "turn1 expected answer (no ask for leave procedure)"
grep -q '"cited": \["' "$TMP/t1.txt" || fail "answer without citation"
echo "3-4 answer ok"
# 8. replay
chat "$T" "$R1" "$Q1" "$TMP/t1r.txt"
grep -q '"replay": true' "$TMP/t1r.txt" || fail "replay flag"
echo "8 replay ok"
# 5. out of scope
chat "$(uuid)" "$(uuid)" "오늘 비트코인 사도 될까요?" "$TMP/t3.txt"
[ "$(outcome "$TMP/t3.txt")" = "fallback" ] || fail "out of scope expected fallback"
echo "5 fallback ok"
echo "SMOKE PASS"

# 04. API 설계

FastAPI(`backend/app/main.py`). 모든 응답은 UTF-8. 학생 API는 인증 없음, 관리자 API는 **Google 로그인 ID 토큰**(`Authorization: Bearer`, 서버 검증 — §6).
요청·응답 스키마의 단일 원본은 `backend/api/schemas.py`(Pydantic)이며, 이벤트 계약 버전은 **`schema_version = 1`**.

## 1. 엔드포인트 목록

| 메서드 | 경로 | 인증 | 설명 |
|---|---|---|---|
| POST | `/api/chat` | 없음 | 질문 1턴 처리, **SSE 스트림** 응답 |
| POST | `/api/feedback` | 없음 | 👍/👎 |
| POST | `/api/track` | 없음 | 클릭 이벤트 |
| GET | `/api/status` | 없음(`deep=1`은 관리자) | 버전·상태(워밍업·점검 겸용) |
| GET | `/api/suggestions` | 없음 | 추천 질문 칩 목록 |
| GET | `/api/admin/unanswered` | 관리자 | 미응답 질문 목록 |
| GET | `/api/admin/feedback` | 관리자 | 부정 피드백 목록 |
| GET | `/api/admin/stats` | 관리자 | 일자별 질문 수·결과 유형 분포·응답시간 |
| GET | `/`, `/admin`, `/privacy` | 없음 | 정적 화면 |

## 2. `POST /api/chat`

### 요청

```json
{
  "schema_version": 1,
  "thread_id": "3f2c…-uuid",
  "request_id": "9a1b…-uuid",
  "message": "휴학하려고 하는데 어떻게 해요?"
}
```

| 필드 | 규칙 |
|---|---|
| `thread_id` | UUID v4 필수. 프론트가 발급해 브라우저에 보관, [새 대화 시작] 시 재발급 |
| `request_id` | UUID v4 필수. **메시지 전송마다 프론트가 새로 발급**, [다시 시도]는 같은 값 재사용 → 멱등 키 |
| `message` | 1~500자. 공백만 있으면 400 |
| `schema_version` | 1이 아니면 400(`SCHEMA_MISMATCH`) — 프론트·백엔드 배포 불일치 조기 발견 |

- 학생 조건(`profile`)은 요청에 넣지 않는다. 되묻기에 대한 답도 이 엔드포인트로 보낸다.
- `turn_id = "{thread_id}_{request_id}"` (서버가 조합, `done`으로 알려 줌)

### 2-1. 처리 순서 (그래프 실행 전·후, API 계층)

**턴 상태**: `processing` → `done` / `failed`(재시도 가능). `turns` 문서에 `status`, `lease_until`(처리 임대 만료), `attempt`(처리 시도 횟수)를 둔다.

```text
1) 입력 검증 → 실패 시 400 JSON (스트림 시작 전)
   query_for_model = mask(message)            # 02 문서 5절 — 이후 원문 미사용
2) 하나의 Firestore 트랜잭션으로 멱등 확인 + 잠금 획득
   transaction:
     u = turns/{turn_id} 읽기
     if u.status == "done":                   → 트랜잭션 종료, final_payload 재전송(아래 4-R)
     if u.status == "processing" and u.lease_until > now:
                                              → 409 THREAD_BUSY (같은 요청이 처리 중)
     # 여기부터 새 처리 또는 재처리(없음 / failed / 임대 만료된 processing = 회수 reclaim)
     t = threads/{thread_id} 읽기
     if t.processing_until > now and t.lock_owner != request_id:
                                              → 409 THREAD_BUSY (같은 대화의 다른 요청이 처리 중)
     t.lock_owner = request_id; t.processing_until = now + 30초
     u = {status:"processing", lease_until: now + 30초, attempt: u.attempt+1 (없으면 1),
          created_at(최초만), expires_at}
3) 스트림 시작(200, 2-2 헤더) → 그래프 실행(마감 25초)
4) 정상 완료:
   - **하나의 Firestore 트랜잭션**으로 다음을 원자적으로 기록(#415·#447):
       turns/{turn_id}: status="done", final_payload, side_effects_done=true
       threads/{thread_id}: profile, pending_question, clarification_count, last_turn (02 §4-10이 계산)
       unanswered upsert(count += 1) — 해당 fallback_reason일 때만
       통계 카운터 갱신
     트랜잭션 시작 시 side_effects_done==true면 부수 효과를 다시 쓰지 않는다
   - 이 트랜잭션이 커밋되기 전에 프로세스가 죽으면 아무것도 반영되지 않으므로(원자성), 재처리해도 count가 두 번 오르지 않는다
4-R) 재전송(done):
   - final_payload를 meta(replay=true) → evidence(인용 카드) → answer|ask|fallback → done 순으로 전송. 모델 호출·통계 재실행 없음
5) finally (정상·오류·연결 끊김 모두):
   - 트랜잭션으로 lock_owner == request_id 인 경우에만 잠금 해제
   - 완료되지 못했으면 turns 문서 status="failed" (재시도 가능)
```

- **연결 끊김**: FastAPI 스트리밍은 클라이언트가 끊긴 뒤 그래프 완료를 보장하지 않는다. 8일 MVP에서는 별도 작업자(워커)를 두지 않고, 끊기면 `failed`로 마감한 뒤 **같은 `request_id` 재요청으로 재처리**한다(#406 합의). 완료 전 부수 효과를 쓰지 않으므로 재처리해도 중복 집계가 없다.
- **프로세스가 죽은 경우**: `status="processing"`이 남아도 `lease_until`(30초)이 지나면 같은 `request_id` 재요청이 회수(reclaim)해 재처리한다 — 영구 409가 되지 않는다.
- `final_payload`에는 답변·되묻기·fallback 데이터와 함께 **인용된 EvidenceCard 전체**를 저장한다(재전송 시 근거 카드 복원).
- `attempt`는 로그·평가에서 재시도 빈도 확인용.

### 2-2. 응답 헤더

```text
Content-Type: text/event-stream; charset=utf-8
Cache-Control: no-cache, no-transform
X-Content-Type-Options: nosniff
X-Accel-Buffering: no
```

### 2-3. 이벤트 순서

```
meta → status(classify) → status(act: search_academic_knowledge) → evidence
     → status(act: get_notices) → evidence → status(compose) → status(verify) → answer → done
```

되묻기: `meta → status(classify) → ask → done` / fallback: `… → fallback → done` / 스트림 시작 후 오류: `… → error → done`(**`error` 뒤에도 반드시 `done`**)

### 2-4. 이벤트 스키마

| event | data |
|---|---|
| `meta` | `{"schema_version": 1, "turn_id": "…", "replay": false, "attempt": 1}` — 첫 이벤트. 멱등 재전송이면 `replay: true` |
| `status` | `{"step": "classify\|act\|compose\|verify", "msg": "학칙 검색 중", "tool": "search_academic_knowledge\|null", "count": 3, "ok": true, "stale": false, "corrections": [{"from": "휴악", "to": "휴학"}]}` — `corrections`는 classify 단계에서 **내용어가 바뀐 보정만** 포함 |
| `evidence` | `{"items": [EvidenceCard]}` — 이번에 새로 가져온 **검색 후보**(누적 아님). 모든 카드 `state: "candidate"` |
| `ask` | `{"question": "학년과 이번 학기 장학금 수혜 여부를 알려 주세요.", "missing_slots": ["grade","scholarship"], "choices": {"grade": ["1","2","3","4","5 이상"], "scholarship": ["예","아니오","모름"]}}` |
| `answer` | `{"notices": [{"code": "source_conflict", "text": "(서버 템플릿 문장)"}], "sentences": [{"text": "...", "cite_ids": ["101_main_32"]}], "checklist": [...], "next_actions": [{"text": "...", "cite_ids": [...]}], "notice": "최종 내용은 원문에서 확인하세요", "dept": Dept\|null, "cited": ["101_main_32"], "as_of": "…"\|null, "stale_used": false}` |
| `fallback` | `{"reason": "out_of_scope\|no_evidence\|verification_failed\|tool_failure\|deadline", "message": "확인된 규정·공지에서 답을 찾지 못했습니다.", "dept": Dept\|null}` |
| `done` | `{"turn_id": "…", "outcome": "answer\|ask\|fallback\|error", "elapsed_ms": 4200, "version": "0.3.0"}` |
| `error` | `{"code": "LLM_UNAVAILABLE\|TIMEOUT\|INTERNAL", "message": "잠시 후 다시 시도해 주세요."}` |

```json
EvidenceCard = {
  "id": "101_main_32", "kind": "article", "state": "candidate",
  "title": "(학칙명) 제32조(휴학)",
  "snippet": "본문 앞 120자",
  "url": "https://…", "department": "학사지원팀",
  "revision_date": "2025-03-01", "has_table": false, "as_of": null, "stale": false
}
Dept = { "dept_id": "acad_support", "name": "학사지원팀", "phone": "055-…", "duties": "교육과정, 성적, 졸업", "location_text": "본관 1층"|null, "source_url": "https://…", "snapshot_at": "2026-09-29" }   // location_text는 장소 표의 검수된 값(지도·좌표 없음)
```

- **후보와 인용 근거 구분**: 스트리밍 중 카드는 모두 "검색 후보"다. `answer.cited`에 포함된 카드만 화면에서 "사용된 근거"로 강조하고, 나머지는 접는다(05 문서).
- `answer`에는 `supporting_quotes`를 내보내지 않는다(내부 검증용).
- `dept`는 서버가 계산한 값이다(02 문서 4-8).
- JSON은 한 줄로 직렬화한다(`data:` 줄 안에 개행 금지). SSE 연결 유지: 15초마다 주석 줄(`: ping`).

## 3. `POST /api/feedback`

```json
{ "thread_id": "…", "turn_id": "…", "rating": 1, "comment": "선택, 200자 이하" }
```
- `rating ∈ {1, -1}`. 문서 ID는 **`turn_id`**(이미 `{thread_id}_{request_id}`) → 같은 턴에 다시 보내면 덮어씀. 응답 `204`.

## 4. `POST /api/track`

```json
{ "thread_id": "…", "turn_id": "…(선택)", "event": "card_click|source_click|chip_click|new_thread", "target": "101_main_32", "card_state": "candidate|cited" }
```
- `turn_id`는 **선택** 필드(`new_thread`·첫 화면 `chip_click`에는 없음). 응답 `204`. 실패해도 프론트는 무시(fire-and-forget, `keepalive`).

## 5. `GET /api/status`

```json
{ "status": "ok", "version": "0.3.0", "schema_version": 1, "model": "gemini-3.5-flash", "index": "articles-v3", "time": "…" }
```
- `?deep=1`이면 Gemini 1회·검색 1회·Firestore 읽기/쓰기까지 수행. 외부 호출과 쓰기가 발생하므로 **라우터 수준에서 관리자 인증을 강제**한다(점검 스크립트는 서비스 계정 ID 토큰 사용).

## 6. 관리자 API (#450~#457 합의)

**인증**: Google 로그인 → 프론트가 받은 **ID 토큰을 서버가 검증**(`aud`, `email_verified=true`, 이메일이 허용 목록 `ADMIN_EMAILS`에 있음). 공유 관리자 키는 폐기. MVP는 허용 이메일 = 전 권한 1역할(팀 3명), `purge`만 2단계 확인. 모든 변경 요청은 `admin_audit`에 기록(actor·action·target·before·after·reason·request_id·created_at·result).

| 메서드·경로 | 용도 | 비고 |
|---|---|---|
| `GET /api/admin/sources` | 출처별 상태: 유형, 활성 여부, 주기, 마지막 성공/실패, 다음 예정, 문서 수, 활성 색인 버전 | |
| `PATCH /api/admin/sources/{id}` | **주기 preset**(`manual`/`daily`/`weekly`), **일시정지/재개** | 임의 cron 문자열 금지 |
| `POST /api/admin/ingestion-runs` | **변경분 강제 재수집**(출처 1개 또는 전체) `{source_ids[], idempotency_key}` | 실행 중이면 기존 `run_id` 반환(중복 클릭 멱등). 웹 프로세스가 직접 수집하지 않고 **수집 Job을 비동기 시작**. 전체 재색인은 CLI(P2) |
| `GET /api/admin/ingestion-runs/{run_id}` | 진행 상태 | **구조화 필드만**: `status`, `phase`, `processed/total`, `added/changed/rejected`, `started_at/finished_at`, 안전한 `error_code`·짧은 메시지. 원시 로그 노출 금지(Cloud Logging에서만) |
| `POST /api/admin/documents` | 교내 문서 업로드 → **staging** | 필수 메타: 제목, 소관부서, 문서일/시행일, 출처, **공개 답변 인용 승인 근거**, 만료일. 형식·크기·해시 검사 |
| `GET /api/admin/documents/{id}/preview` | 변환·조 분할 결과 미리보기 | |
| `POST /api/admin/documents/{id}/publish` | 색인 반영 | 승인 근거 없으면 거부 |
| `POST /api/admin/documents/{id}/reject` · `/archive` | 거부 / 게시 중단(기본 삭제 방식) | archive = 검색 제외 + 버전 보존 |
| `POST /api/admin/documents/{id}/purge` | 완전 삭제(원본·추출본·색인·캐시) | 개인정보 오업로드·법적 요청 전용, 2단계 확인 |
| `POST /api/admin/disable` | **긴급 회수**: 이미 공개된 문서·조문을 즉시 검색에서 제외 | 서버 denylist(`disabled_document_ids`)에 즉시 기록 + Vertex 비활성화 작업 병행(03 §1) |
| `GET /api/admin/unanswered` · `/feedback` · `/stats` | 기존 운영 정보 | `limit`(≤200)·`cursor`, 기간 ≤90일 |
| `GET/POST/PATCH /api/admin/places` | 장소 표 조회·수기 등록·수정·검수(`pending`→`verified`) | 원문 URL·확인일 필수, 감사 로그 |
| `GET /api/admin/glossary` | 적용 중인 glossary **읽기 전용**(버전·커밋·적용 시각) | 편집은 코드 리뷰 경로로만 |
| `GET /api/admin/audit` | 감사 로그 조회 | |

- `/api/status?deep=1`도 관리자 인증 필요.
- 필요한 Firestore 복합 인덱스는 배포 전 생성.
- **P2(후순위 구현 — 최종 범위에 포함)**: 관리자 전용 서비스 분리, 역할 세분화(viewer/operator/publisher), 버전 rollback UI, dry-run, restricted 문서 검색 경로(재학생 인증이 도입될 때)

## 7. 공통 오류 응답 (스트림 시작 전·JSON 엔드포인트)

| HTTP | code | 상황 |
|---|---|---|
| 400 | `BAD_REQUEST` / `SCHEMA_MISMATCH` | 필드 누락·형식 오류·길이 초과 / 계약 버전 불일치 |
| 401 | `UNAUTHORIZED` | 관리자 ID 토큰 없음·검증 실패·허용 목록 밖 |
| 409 | `THREAD_BUSY` | 같은 대화에서 처리 중(잠금 보유 중이거나 같은 요청 처리 중) |
| 429 | `RATE_LIMITED` | IP당 분당 20회 초과 — **인스턴스별 메모리 카운터라 최선 노력(best-effort) 수준**. 클라이언트 IP는 Cloud Run 앞단이 붙인 `X-Forwarded-For`의 첫 값만 신뢰 |
| 500 | `INTERNAL` | 기타 |

- 스트림이 시작된 뒤에는 HTTP 상태를 바꿀 수 없으므로 `error` 이벤트 + `done`으로 알린다.

## 8. 응답 헤더 정책 (미들웨어)

| 대상 | 헤더 |
|---|---|
| `.html`, `.js`, `.css` | `Cache-Control: no-cache` (배포 직후 바로 반영) |
| 이미지·폰트 | `Cache-Control: public, max-age=604800` |
| `/api/*` | `Cache-Control: no-store` |
| 모든 HTML | `Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin` |

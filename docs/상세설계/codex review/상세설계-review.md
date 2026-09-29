# 캠퍼스 브릿지 상세설계 Codex 리뷰

- 검토일: 2026-09-28
- 검토 범위: `docs/상세설계/00-개요.md` ~ `08-테스트-설계.md`
- 검토 관점: 문서 간 계약 일치, 실제 구현 가능성, 장애·재시도 안전성, 평가 신뢰성, 8일 MVP 범위 적합성
- 원칙: 기능 범위를 늘리지 않고 구현 중 재작업을 유발할 항목을 우선 지적한다.

## 총평

상세설계는 모듈, 데이터 스키마, API 이벤트, 테스트 항목까지 연결되어 있어 바로 구현을 시작할 수 있는 수준이다. 특히 다음 부분이 좋다.

- 규정 수집과 웹 서비스를 분리했다.
- 조문 단위 색인과 근거 카드의 메타데이터가 연결되어 있다.
- Firestore를 사용해 Cloud Run의 인스턴스 재시작 문제를 보완했다.
- `supporting_quotes`와 숫자 검증을 도입해 단순 인용 ID 검사보다 강하게 설계했다.
- 개발셋·최종셋·범위 밖 세트를 분리했다.
- smoke test, 롤백, 비용 상한까지 설계에 포함했다.

다만 구현 전에 반드시 정리해야 할 핵심 문제는 다음과 같다.

1. Vertex AI Search 예시의 `content.rawText`가 현재 공식 가져오기 예시와 맞지 않을 가능성이 높다.
2. Firestore TTL 필드로 `created_at`을 쓰면 90일 보존이 아니라 생성 직후 만료 대상으로 처리될 수 있다.
3. 도구 정상 반환형과 실패 반환형이 서로 다르다.
4. SSE 연결 중단 후 재시도에 대한 멱등성이 없어 중복 처리·로그가 생길 수 있다.
5. Firestore의 대화 잠금이 트랜잭션으로 정의되지 않아 동시 요청 경쟁이 남는다.
6. `next_actions`와 `dept`는 근거 검증을 거치지 않아 사실이 아닌 부서·행동이 출력될 수 있다.
7. 현재 저장 상태만으로는 일반적인 후속 질문을 이해할 수 없다.
8. 웹 이미지에서 LibreOffice 변환을 어떻게 제공할지 정의되어 있지 않다.

---

## P0 — 구현 전에 수정할 항목

## 1. Vertex AI Search 가져오기 형식 확정

대상: `01-데이터-설계.md` 2절

현재 예시는 다음 구조다.

```json
{
  "id": "195_main_32",
  "structData": { "...": "..." },
  "content": {
    "mimeType": "text/plain",
    "rawText": "제32조(휴학) ① …"
  }
}
```

현재 Google 공식 문서의 비정형 데이터 가져오기 예시는 `content.uri`로 Cloud Storage 파일을 가리킨다. API의 인라인 콘텐츠는 `rawBytes` 계열을 사용하며 JSON에서는 base64 처리가 필요하다. 따라서 `rawText`를 전제로 전체 JSONL을 만들면 첫날 가져오기 단계에서 실패할 가능성이 있다.

권장 결정안은 둘 중 하나다.

### 안 A — 비정형 TXT 문서

- 조문마다 `gs://.../articles/{article_id}.txt` 파일 생성
- 메타데이터 JSONL의 `content.uri`가 TXT 파일을 참조
- `structData`에는 규정명·부서·개정일·접근등급 저장
- 검색 결과에서 원문 전체와 메타데이터를 함께 복원

### 안 B — 구조화 데이터

- 조문 본문을 `content` 문자열 필드로 포함하는 구조화 JSONL 사용
- 사용자 정의 스키마에서 검색 가능·필터 가능 필드를 명시
- 검색 품질과 본문 반환 형태를 20개 문서 Spike로 확인

1일차 Search Spike의 성공 기준도 구체화한다.

1. 20개 조문 import 성공
2. `access="public"` 필터 성공
3. 대표 질문 5개 중 기대 조문 Recall@5 4개 이상
4. 검색 결과에서 본문·원문 URL·부서·개정일 복원 성공
5. 같은 ID 재가져오기 시 갱신 방식 확인

참고:

- [Google Agent Search 데이터 준비](https://docs.cloud.google.com/generative-ai-app-builder/docs/prepare-data)
- [Google Agent Search 사전 파싱 문서 가져오기](https://docs.cloud.google.com/generative-ai-app-builder/docs/parse-chunk-documents)

## 2. Firestore TTL 필드 수정

대상: `01-데이터-설계.md` 5절, `07-인프라-배포-설계.md` 1절

현재 설계는 `turns`, `unanswered`, `feedback`, `events`의 TTL 필드로 `created_at`을 적고 있다. Firestore TTL 필드는 문서의 **만료 시각**이어야 하므로 생성 시각을 TTL 필드로 지정하면 이미 지난 시각으로 판단될 수 있다.

모든 만료 대상 컬렉션에 명시적인 `expires_at`을 둔다.

| 컬렉션 | 권장 TTL 값 |
|---|---|
| `threads` | `now + 24시간` |
| `turns` | `created_at + 90일` |
| `feedback` | `created_at + 90일` |
| `events` | `created_at + 90일` |
| `unanswered` | `last_at + 90일`로 갱신 |
| 캐시 컬렉션 | 캐시별 TTL만큼 `expires_at` 설정 |

Firestore TTL 삭제는 만료 시각에 정확히 즉시 실행되는 것이 아니라 보통 이후 비동기로 처리되므로, 애플리케이션 조회에서도 `expires_at > now` 조건을 지켜야 한다.

참고: [Firestore TTL 정책](https://docs.cloud.google.com/firestore/native/docs/ttl)

## 3. 도구 반환 계약 통일

대상: `03-도구-설계.md` 공통 절

문서 시작에서는 모든 도구가 `list[Evidence]`를 반환한다고 했지만 실패 시에는 다음 객체를 반환하도록 되어 있다.

```json
{"ok": false, "reason": "...", "items": []}
```

이 상태로는 `ToolNode` 결과를 공통 변환하기 어렵고 타입 검사도 깨진다. 모든 도구가 동일한 envelope을 반환하도록 한다.

```python
class ToolResult(BaseModel):
    ok: bool
    items: list[Evidence] = []
    error_code: str | None = None
    message: str | None = None
    as_of: datetime | None = None
    stale: bool = False
```

- 성공: `ok=True`, `items=[...]`
- 정상적인 빈 결과: `ok=True`, `items=[]`, `message="식단 없음"`
- 장애: `ok=False`, `items=[]`, `error_code="UPSTREAM_TIMEOUT"`
- 마지막 정상 캐시: `ok=True`, `stale=True`, `as_of=...`

Agent는 `ok=False`를 evidence로 만들지 않고 상태 이벤트에만 반영한다.

## 4. 요청 멱등성과 SSE 재연결

대상: `04-API-설계.md` 2절, `05-화면-설계.md` 1절

현재 `turn_id`는 서버가 발급한다. 브라우저가 응답을 받기 전에 SSE 연결이 끊기면 같은 메시지를 다시 보내게 되고, 서버는 새로운 턴으로 처리한다. 이 경우 Gemini 중복 호출, `unanswered` 중복 증가, 피드백 연결 오류가 생길 수 있다.

요청에 프론트가 발급하는 `request_id`를 추가한다.

```json
{
  "thread_id": "uuid",
  "request_id": "uuid",
  "message": "휴학하려고 하는데 어떻게 해요?"
}
```

- Firestore `turns` 문서 ID를 `request_id` 또는 `{thread_id}_{request_id}`로 사용
- 동일 요청이 완료된 경우 저장된 최종 결과를 재전송
- 처리 중이면 409 또는 진행 상태 반환
- 실패 후 재시도해도 `unanswered`, 통계, 모델 호출이 중복되지 않도록 설계
- 프론트의 `[다시 시도]`는 같은 `request_id`를 사용

Cloud Run은 스트리밍 응답을 지원하지만 연결이 끊겨 재요청될 수 있으므로 요청 멱등성을 갖추는 것이 안전하다. [Cloud Run 요청 시간 제한](https://docs.cloud.google.com/run/docs/configuring/request-timeout)

## 5. 대화 잠금의 원자성

대상: `01-데이터-설계.md`의 `processing_until`, `04-API-설계.md`의 409 처리

`processing_until`을 단순 읽기 후 쓰기로 처리하면 두 요청이 동시에 빈 값을 읽고 모두 잠금을 획득할 수 있다. Firestore 트랜잭션으로 다음을 원자적으로 수행해야 한다.

```text
transaction:
  threads/{thread_id} 읽기
  processing_until > now 이면 THREAD_BUSY
  아니면 lock_owner=request_id, processing_until=now+30초 기록
```

해제는 `finally`에서 수행하되, 현재 `lock_owner`가 자신의 `request_id`와 같은 경우에만 지운다. 프로세스가 죽어도 `processing_until`이 지나면 다시 처리할 수 있어야 한다.

참고: [Firestore 트랜잭션](https://docs.cloud.google.com/firestore/native/docs/manage-data/transactions)

## 6. 검증 범위 확대

대상: `02-Agent-설계.md` 4-7절

현재 `verify_sentence()`에 다음 보완이 필요하다.

### 배열 길이 검사

`zip(cite_ids, supporting_quotes)`는 길이가 짧은 쪽에 맞춰 조용히 잘린다. 반드시 다음 조건을 먼저 확인한다.

```python
if not s.cite_ids:
    return False
if len(s.cite_ids) != len(s.supporting_quotes):
    return False
```

### `next_actions` 검증

현재 `next_actions: list[str]`는 검증 대상이 아니다. 다음 문장도 사실 주장이다.

- "학사지원팀에 문의하세요."
- "포털에서 신청하세요."
- "본관 2층을 방문하세요."

`next_actions`도 `DraftSentence`로 바꾸거나 서버 템플릿으로 생성해야 한다.

권장:

- 신청 방법·기한·부서가 포함된 행동: 인용 필수
- "원문을 최종 확인하세요" 같은 일반 안전 안내: 템플릿으로만 허용

### `dept` 검증

`draft.dept`를 모델이 자유 생성하게 두지 않는다.

1. 인용된 evidence의 `department` 최빈값
2. 위치표의 정규화된 부서 ID로 변환
3. 없으면 의도별 기본 부서를 "일반 안내"로 명시

### 의미 충실성의 한계 표시

현재 결정적 검증은 인용 구간 존재와 숫자 일치만 확인한다. 부정 표현, 적용 대상, 예외 조건이 뒤집힌 문장은 통과할 수 있다. 문서에 다음 한계를 명시하고 평가에서 수동 Faithfulness 검사를 유지한다.

## 7. 실제 대화 문맥 범위 확정

대상: `01-데이터-설계.md`의 `threads`, `02-Agent-설계.md`의 State

현재 Firestore에는 프로필, 미완료 질문, 마지막 의도만 저장한다. 따라서 다음 후속 질문은 해석할 수 없다.

```text
사용자: 휴학 신청 기간이 언제예요?
Agent: ...
사용자: 그건 어디서 신청해요?
```

둘 중 하나를 명확히 선택해야 한다.

### 권장 MVP안

`threads`에 최소 문맥만 추가한다.

```text
last_user_query
last_answer_summary
last_cited_ids[]
last_topic
clarification_count
```

- 최근 1턴만 유지
- 모델 입력 전 길이 제한
- 24시간 TTL 적용
- 개인정보 마스킹 후 저장

일반 후속 질문을 지원하지 않기로 결정한다면 UI와 문서에 "절차 되묻기만 이어서 처리"한다고 한계를 명시한다.

## 8. 되묻기 1회 제한 구현

대상: `02-Agent-설계.md` 4-2~4-3절

문서에는 되묻기를 한 번만 한다고 되어 있지만 State에는 횟수 필드가 없다. 두 번째 답변에서도 slot이 부족하면 현재 라우팅은 다시 `ask_user`로 간다.

다음 중 하나를 저장한다.

- `clarification_count: int`
- `asked_slots: list[str]`

라우팅은 다음과 같이 명시한다.

```python
if missing_slots and clarification_count == 0:
    return "ask_user"
if missing_slots:
    return "expand_query"  # 일반 정보로 답하고 조건별 차이를 명시
```

`scholarship` 타입도 `bool`만으로는 선택지의 "모름"을 표현할 수 없으므로 `Literal["yes", "no", "unknown"] | None`으로 바꾼다.

## 9. HWP 변환 실행 환경 결정

대상: `01-데이터-설계.md` 1-3절, `07-인프라-배포-설계.md` 6·8절

Cloud Run Job이 HWP 변환을 수행하지만 Dockerfile은 `python:3.12-slim + uv`로만 설명되어 있다. LibreOffice fallback을 실제로 사용하려면 이미지에 LibreOffice가 설치되어야 하며 이미지 크기와 빌드 시간이 크게 늘 수 있다.

8일 MVP에서는 다음 중 하나를 권장한다.

### 권장안

- 1차 변환과 전체 초기 색인은 로컬에서 실행
- 검증된 JSONL/TXT만 GCS로 업로드
- Cloud Run Job은 변경 감지와 `hwp5txt` 성공 파일만 처리
- LibreOffice가 필요한 파일은 `rejected`로 표시하고 로컬 수동 변환

### 대안

- `web`과 `ingest` 이미지를 분리
- ingest 이미지에만 LibreOffice 설치
- Cloud Build에서 실제 HWP 픽스처 변환 테스트 수행

어느 방식을 택하든 `hwp5txt --version`, LibreOffice 존재 여부, Python 3.12 호환성을 1일차 Spike에서 확인한다.

---

## P1 — 구현 중 반영할 항목

## 10. 범위 밖 질문과 미응답 질문 분리

대상: `02-Agent-설계.md`의 fallback

현재 모든 fallback이 `unanswered`에 누적된다. 그러면 날씨, 연예인, 코딩 질문 같은 명백한 범위 밖 질문이 "학생들이 찾지 못한 학교 정보" 대시보드에 섞인다.

`fallback_reason`을 추가한다.

```text
out_of_scope
no_evidence
tool_failure
verification_failed
```

- `unanswered`에는 `no_evidence`, `verification_failed`만 기본 집계
- `out_of_scope`는 통계만 기록하고 미응답 목록에서 제외
- `tool_failure`는 운영 오류로 별도 집계

## 11. 오래된 캐시의 답변 제한

대상: `01-데이터-설계.md` 6절, `03-도구-설계.md`

공지의 마지막 정상값을 최대 7일 사용하는 것은 시연 안정성에는 좋지만 마감·신청기간 질문에 오래된 정보를 답할 위험이 있다.

- `stale=true`, `as_of`, `age_seconds`를 명시
- 마감·신청기간·오늘·현재 질문에서 stale 공지는 확정 답변에 사용하지 않음
- 오래된 값은 "마지막 확인 정보"로만 표시하고 원문 재확인을 안내
- 식단은 지난 캐시를 다른 날짜 답변에 절대 사용하지 않음

## 12. API 세부 계약 충돌

대상: `01`, `04`, `05` 문서

### 피드백 문서 ID

- 데이터 설계: `feedback` 문서 ID는 자동
- API 설계: 같은 `turn_id`의 피드백은 덮어씀

덮어쓰려면 문서 ID를 `{thread_id}_{turn_id}`로 정하거나 트랜잭션 조회 후 갱신해야 한다. 전자가 단순하다.

### `new_thread` 이벤트

- `/api/track` 요청은 `turn_id`를 필수처럼 표현한다.
- 화면은 새 대화 시작 직후 `track(new_thread)`를 호출한다.

이때 아직 turn이 없으므로 `turn_id`를 선택 필드로 바꾸거나 `new_thread` 이벤트를 제거한다.

### SSE 오류

- HTTP 응답 헤더 전 오류: 400·409·429 JSON 응답 가능
- 스트림 시작 후 오류: HTTP 상태를 바꿀 수 없으므로 `error` 후 반드시 `done` 이벤트 전송
- 클라이언트 disconnect 시 잠금 해제와 취소 처리를 `finally`에서 수행

### 이벤트 버전

`done.version`은 앱 버전이고 이벤트 계약 버전은 아니다. `schema_version: 1`을 요청 또는 첫 이벤트에 추가하면 프론트와 백엔드 불일치를 빨리 발견할 수 있다.

## 13. 검색 후보와 최종 인용 카드 구분

대상: `04-API-설계.md`, `05-화면-설계.md`

현재 도구 호출 직후 모든 검색 결과를 `evidence` 이벤트로 화면에 추가한다. 최종 답변은 일부 근거만 인용하므로 사용자는 검색 후보 전체를 실제 답변 근거로 오해할 수 있다.

권장 처리:

- 스트리밍 중 카드는 "검색 후보" 상태로 표시
- 최종 `answer.cited`에 포함된 카드만 "사용된 근거"로 강조
- 인용되지 않은 카드는 접거나 제거
- 카드 클릭 로그에도 `candidate`와 `cited`를 구분

## 14. 프론트엔드 출력 안전성

대상: `05-화면-설계.md`

- 답변·공지 제목·부서명은 `innerHTML`이 아니라 `textContent`로 렌더링
- URL은 허용 프로토콜 `https:`와 허용 도메인을 모두 검사
- 새 탭 링크에 `rel="noopener noreferrer"`
- Content-Security-Policy, `X-Content-Type-Options: nosniff`, `Referrer-Policy` 설정
- 구조화 출력의 Markdown을 지원하지 않는다면 평문 렌더링으로 고정
- 스트림 완료 후 답변으로 포커스를 이동하거나 스크린리더 알림 제공

## 15. 외부 호출의 전체 시간 예산

대상: `07-인프라-배포-설계.md` 5절

`timeout=30초`, 최대 5회 재시도, 지수 백오프를 도구마다 적용하면 한 턴이 수십 초에서 수분까지 늘어날 수 있다. 목표 응답시간 10초와 충돌한다.

다음 두 제한을 구분한다.

- 개별 호출 제한: 예) 검색 5초, 학교 사이트 5초, Gemini 15초
- 턴 전체 deadline: 예) 25초

남은 시간이 부족하면 추가 도구 호출과 재시도를 중단하고 확보한 근거로 compose하거나 오류를 반환한다. Cloud Run timeout 300초는 플랫폼 상한일 뿐 Agent 처리 목표가 아니다.

## 16. 관리자 키와 운영 API

대상: `04`, `05`, `07` 문서

- `ADMIN_KEY`는 일반 환경변수 값 직접 입력보다 Secret Manager 참조로 주입
- 키 비교는 상수 시간 비교 사용
- 관리자 API에 `limit` 상한과 pagination cursor 추가
- 필요한 Firestore 복합 인덱스를 배포 전에 생성
- `GET /api/status?deep=1`은 외부 호출과 쓰기를 발생시키므로 관리자 전용임을 라우터 수준에서 강제
- 관리자 화면에 키를 `sessionStorage`로 보관하는 것은 MVP에서 허용하되 CSP와 XSS 방어를 전제로 함

## 17. 위치 데이터의 안정된 키

대상: `01-데이터-설계.md` 4절, `03-도구-설계.md` 5절

규정의 `department`와 CSV의 `dept`를 동일 문자열로 맞추는 방식은 명칭 변경과 띄어쓰기에 취약하다.

```text
dept_id, canonical_name, aliases, building, floor, ...
```

- 내부 연결은 `dept_id`
- 화면은 `canonical_name`
- 규정 원문의 부서명은 alias로 정규화
- fuzzy match 결과가 2개 이상이면 임의 선택하지 않고 후보 반환
- `loc:{dept}` ID에는 공백·특수문자 대신 `dept_id` 사용
- 카카오맵 링크의 이름은 URL 인코딩

## 18. 규정 분할 ID 충돌과 예외

대상: `01-데이터-설계.md` 1-4~1-6절

- 같은 날짜의 부칙이 둘 이상이면 `{date}_{조}`만으로 충돌할 수 있으므로 부칙 순번 또는 revision 번호 포함
- 날짜를 읽지 못한 부칙의 ID 규칙 필요
- 2,000자 초과 조에 항 번호가 없을 때 사용하는 순차 청크 ID 필요
- 삭제된 조문·조 번호 결번은 실제 규정에서 정상일 수 있으므로 연속성 실패는 경고로 유지
- 별표·별지 SKIP 상태에서 다음 조·부칙을 만났을 때 정상 복귀하는 테스트 추가
- 변환 텍스트가 비정상적으로 길어지는 경우도 검수 항목에 포함

---

## 문서별 리뷰

## `00-개요.md`

### 수정 필요

- 상위 문서 경로 `../구현전략/구현전략.md`는 실제 구조와 다르다. `../구현전략.md`가 맞다.
- 공통 규약에 API/SSE `schema_version`을 추가한다.
- 개인정보 규약에 보존 기간뿐 아니라 `expires_at` 필드 사용을 명시한다.

### 유지 권장

- 요구사항 추적표는 구현 후에도 테스트 파일과 연결해 유지한다.
- 각 요구사항에 상태(`planned/implemented/verified/cut`)를 추가하면 기능 축소 이력 관리에 도움이 된다.

## `01-데이터-설계.md`

### 장점

- 단계별 파이프라인과 검수 실패 시 기존 색인을 유지하는 안전장치가 좋다.
- `content_hash`, ETag, 변환기 버전을 남기는 설계가 재현성에 유리하다.

### 수정 필요

- Search import 형식 확정
- TTL을 `expires_at`으로 변경
- `department` 문자열 대신 `dept_id` 도입
- 색인 버전(`index_version`)과 import 작업 ID 저장
- 새 색인이 완성되기 전 기존 검색 앱을 바로 덮어쓰지 않는 배포 방식 결정

## `02-Agent-설계.md`

### 장점

- 상태 로드·저장 노드를 그래프 안에 포함해 배포 환경과 설계가 일치한다.
- 구조화 출력과 결정적 검증을 분리한 방향이 적절하다.

### 수정 필요

- `llm_calls_count`를 State에 추가하고 실제 증가 지점을 정의
- `in_scope`와 `intent="out_of_scope"`가 충돌할 때의 우선순위 정의
- `extracted_profile: dict` 대신 명시적 Pydantic 모델 사용
- 사용자가 되묻기에 답하지 않고 새 질문을 한 경우 pending 질문을 취소하는 규칙 추가
- `clarification_count` 추가
- `next_actions`, `dept` 검증
- 일반 후속 질문을 위한 최소 문맥 또는 지원 범위 제한
- `fallback_reason` 추가

## `03-도구-설계.md`

### 장점

- URL을 도구 인자로 받지 않는 설계는 SSRF 방어에 효과적이다.
- 캐시와 마지막 정상값을 도구별로 구분한 점이 좋다.

### 수정 필요

- `ToolResult`로 반환 계약 통일
- 모든 입력에 상한 추가: `keyword` 길이, `days`, 날짜 범위, `top_k`
- redirect 후 최종 URL도 allowlist 재검사
- RSS 날짜의 timezone과 날짜 파싱 실패 규칙 정의
- stale 데이터가 마감·오늘 질문에 사용되지 않도록 제한
- 로컬 검색 전환은 런타임 사용자 승인이 아니라 프로젝트 전환 결정으로 표현

## `04-API-설계.md`

### 장점

- SSE 이벤트 유형이 작고 명확하며 화면 설계와 연결되어 있다.
- `/api/status?deep=1`과 smoke test의 연결이 좋다.

### 수정 필요

- `request_id` 추가와 멱등 처리
- Firestore 잠금을 트랜잭션으로 정의
- 스트림 시작 전 오류와 시작 후 오류 구분
- 후보 evidence와 최종 cited evidence 상태 구분
- `/feedback` 덮어쓰기용 문서 ID 확정
- `/track(new_thread)`의 `turn_id` 선택 처리
- IP rate limit은 인스턴스별 best-effort임을 명시하고 프록시 헤더 신뢰 범위 정의
- SSE 응답 헤더와 disconnect cleanup 정의

권장 SSE 헤더:

```text
Content-Type: text/event-stream; charset=utf-8
Cache-Control: no-cache, no-transform
X-Content-Type-Options: nosniff
```

## `05-화면-설계.md`

### 장점

- 모바일 우선, 개인정보 안내 상시 노출, 근거 카드와 처리 타임라인 구성이 시연 목적에 잘 맞는다.

### 수정 필요

- 모든 외부 텍스트를 `textContent`로 렌더링
- 재시도 시 같은 `request_id` 사용
- 스트림 중 후보 근거와 최종 사용 근거를 시각적으로 구분
- 네트워크 중단과 서버 오류 메시지를 구분
- 피드백 중복 클릭 방지와 저장 완료 상태 표시
- 개인정보처리방침에 `localStorage`의 무작위 대화 ID 사용을 명시

## `06-평가-설계.md`

### 장점

- 검색 실패와 생성 실패를 분리할 수 있는 지표 구성이 좋다.
- 평가 캐시 키에 커밋·색인·모델·프롬프트 버전을 모두 포함한 점이 좋다.

### 수정 필요

- 단순 무작위 분할 대신 유형별 층화 분할 사용
- `final.jsonl`은 6일차 기능 동결 전까지 이동우가 별도 보관하고, 최종 평가 시 저장소에 추가하는 편이 holdout 오염을 줄임
- `expected_article_ids`에 `any_of`와 `all_of` 개념 추가
- 실시간 문항도 고정 HTML/RSS fixture 정확성 테스트와 실제 freshness 확인을 분리
- 신청서의 "평균 응답시간 10초"와 현재 `p50 ≤ 10초`가 다르므로 평균·p50·p90을 모두 보고
- `Citation Coverage`는 현재 초안 중 살아남은 문장 비율에 가까우므로 `Verified Retention`으로 이름을 바꾸거나 계산식을 명확히 설명
- 15문항에서 85%를 넘기려면 최소 13문항 정답(86.7%)임을 완료 기준에 명시

## `07-인프라-배포-설계.md`

### 장점

- 서비스 계정 분리, 평시 `min-instances=0`, 배포 후 smoke test가 적절하다.

### 수정 필요

- 실제 IAM 역할 ID를 명시하고 1일차 권한 smoke test 수행
- Secret Manager를 "여유 시"가 아니라 관리자 키의 기본 방식으로 설정
- 웹 이미지와 ingest 이미지/실행 환경 결정
- 외부 호출 전체 deadline 추가
- Cloud Build 트리거 설정이 지연될 경우 `gcloud builds submit` 또는 직접 배포하는 fallback 명령 준비
- rollback 기록에 코드 revision뿐 아니라 `index_version`, Firestore schema version 포함
- 원문 링크 HEAD 요청은 서버가 HEAD를 막을 수 있으므로 GET 또는 실제 브라우저 접근 검사로 대체 가능

## `08-테스트-설계.md`

### 장점

- `verify`를 최우선 테스트 대상으로 잡은 것이 적절하다.
- 실제 변환 텍스트 픽스처를 포함하려는 방향이 좋다.

### 추가할 테스트

| 영역 | 추가 테스트 |
|---|---|
| Search 계약 | 20개 문서 import → 검색 → 본문·메타데이터 복원 |
| TTL | 모든 저장 문서의 `expires_at`이 미래 시각인지 확인 |
| 잠금 | 동시 요청 2개 중 하나만 획득, 만료 잠금 재획득, 다른 owner가 해제 불가 |
| 멱등성 | 같은 `request_id` 재전송 시 모델·통계 중복 없음 |
| SSE | client disconnect 후 잠금 해제, 오류 뒤 `done`, JSON 내 개행 처리 |
| Agent | pending 상태에서 새 주제 질문 시 이전 pending 취소 |
| Verify | cite와 quote 배열 길이 불일치, 부정 의미 뒤집힘 수동 케이스 |
| 도구 | redirect된 외부 도메인 차단, stale 마감 정보 답변 금지 |
| UI | HTML이 텍스트로 렌더링되고 스크립트가 실행되지 않음 |
| HWP | 실제 배포 이미지에서 hwp5txt 실행, fallback 경로 확인 |
| 피드백 | 동일 turn 재전송 시 문서 1개만 유지 |

---

## 권장 공통 계약

구현 전에 아래 스키마를 먼저 코드로 만들고 Agent·API·테스트가 함께 import하도록 하면 문서 간 불일치를 줄일 수 있다.

| 계약 | 권장 위치 |
|---|---|
| `Evidence`, `ToolResult` | `backend/domain/evidence.py` |
| `ChatRequest`, SSE payload | `backend/api/schemas.py` |
| `Profile`, `ThreadState` | `backend/domain/thread.py` |
| `DraftSentence`, `Draft`, `Answer` | `backend/domain/answer.py` |
| 오류 코드 enum | `backend/domain/errors.py` |
| 프론트 이벤트 계약 | `docs/api-events.md` 또는 JS의 중앙 dispatcher |

Pydantic 모델을 단일 원본으로 두고 OpenAPI와 테스트 fixture를 여기서 파생한다.

---

## 권장 구현 순서 조정

현재 8일 계획 안에서 재작업을 줄이는 순서는 다음이 적합하다.

### 1일차 오전

1. GCP 권한·모델 1회 호출 확인
2. HWP 실제 파일 1개 변환
3. 조문 20개 Search import Spike
4. Search 결과를 `Evidence`로 변환

### 1일차 오후

5. `Evidence`, `ToolResult`, `Profile`, `Draft` 공통 모델 작성
6. 조 분할·매니페스트·검수 구현
7. 전체 대상 규정 변환 시작

### 2일차

8. `search_rules` 단독 E2E
9. compose → verify → answer 최소 그래프
10. CLI에서 질문 → 근거 답 확인

### 3일차

11. Firestore thread 상태·트랜잭션 잠금·멱등성
12. FastAPI SSE
13. 웹에서 근거 카드 답변

이 순서에서는 LangGraph의 복잡한 도구 루프보다 **Search import와 공통 데이터 계약**을 먼저 고정한다.

---

## 최종 우선순위

### 반드시 수정 후 구현

1. Search import의 `rawText` 형식 확정
2. Firestore TTL을 `expires_at`으로 변경
3. `ToolResult` 반환형 통일
4. `request_id` 기반 멱등성
5. Firestore 트랜잭션 잠금
6. `next_actions`·`dept` 근거 검증
7. 되묻기 횟수와 최소 문맥 저장
8. HWP 변환 실행 환경 결정

### 3일차 이전 반영

9. fallback 원인 분리
10. stale 캐시 제한
11. API 계약 충돌 해소
12. 후보 근거와 인용 근거 구분
13. 프론트 출력 안전성
14. 턴 전체 시간 예산

### 배포 전 반영

15. Secret Manager와 관리자 API 제한
16. 평가 지표 명칭·평균 응답시간 정합성
17. 추가 동시성·멱등성·SSE 테스트
18. rollback에 색인 버전 포함

## 결론

새로운 사용자 기능을 더 추가할 필요는 없다. 현재 설계에서 가장 큰 위험은 기능 부족이 아니라 **데이터 가져오기 형식과 모듈 간 계약이 구현 시점에 어긋나는 것**이다. P0 항목을 먼저 수정하면 이후 작업은 문서에 적힌 구조를 그대로 따라 구현할 수 있다.

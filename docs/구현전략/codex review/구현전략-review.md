# 캠퍼스 브릿지 구현전략 Codex 리뷰

- 검토일: 2026-09-28
- 검토 대상: `docs/구현전략.md`
- 목적: 8일 개발 기간 안에 MVP를 안정적으로 구현·검증·시연하기 위한 보완 사항 정리

## 총평

현재 구현전략은 문제 정의, MVP 기능, LangGraph 흐름, 데이터 수집, 평가 지표, 일별 일정이 구체적으로 잡혀 있다. 특히 매일 동작하는 End-to-End를 유지하고 기능 우선순위를 명시한 점이 좋다.

추가로 보완할 부분은 기능 확대보다 다음 네 가지에 집중하는 것이 적절하다.

1. Vertex AI Search 초기 연동 실패를 빠르게 발견하는 검증 절차
2. Cloud Run 재시작에도 유지되는 대화 상태
3. 인용의 존재 여부를 넘어 실제 문장과 근거가 일치하는지 확인하는 검증
4. 모델·SDK·API 조합의 명확한 고정

---

## 1. Vertex AI Search 사전 검증 단계

### 현재 위험

구현전략에는 "조 1개 = 문서 1개(JSONL: 본문 + structData)"라고 되어 있으나, 실제 가져오기 방식이 구조화 데이터인지 비정형 문서와 메타데이터 조합인지 명확하지 않다. 전체 규정을 먼저 변환한 뒤 가져오기 형식이 맞지 않는다는 사실을 발견하면 1일차 일정이 크게 지연될 수 있다.

### 권장 보완

전체 수집 전에 조문 20개만 사용하는 Search Spike를 수행한다.

1. 조문 20개를 변환한다.
2. 아래 필드를 포함한 실제 가져오기 스키마를 만든다.
3. 데이터 스토어에 가져온다.
4. 대표 질문 5개로 기대 조문이 검색되는지 확인한다.
5. 성공한 스키마와 명령을 고정한 뒤 전체 변환을 진행한다.

권장 필드:

| 필드 | 용도 |
|---|---|
| `article_id` | 규정번호와 조 번호를 조합한 고유 ID |
| `content` | 검색할 조문 본문 |
| `rule_name` | 규정명 |
| `article_no` | 조 번호 |
| `article_title` | 조 제목 |
| `chapter` | 장·절 정보 |
| `department` | 소관부서 |
| `revision_date` | 개정일 |
| `source_url` | 원문 링크 |
| `access` | `public` 또는 향후 확장용 `internal` |
| `content_hash` | 실제 내용 변경 감지 |

### 전환 기준

- 9월 29일 오전까지 20개 조문 가져오기와 검색을 완료한다.
- 정오까지 Search에서 기대 조문이 검색되지 않으면 원인을 한 번만 수정한다.
- 오후까지 해결되지 않으면 로컬 하이브리드 검색으로 전환한다.
- 전환 사유와 시각을 `docs/ai-usage-log.md` 및 완료보고서에 기록한다.

참고: [Google Agent Search 데이터 준비 문서](https://docs.cloud.google.com/generative-ai-app-builder/docs/prepare-data)

---

## 2. 대화 상태 영속화

### 현재 위험

`MemorySaver`와 Cloud Run 단일 인스턴스를 조합해도 인스턴스 재시작 시 대화 상태와 LangGraph `interrupt()` 정보가 사라질 수 있다. `min-instances=1`은 콜드 스타트를 줄이는 설정이지 프로세스 메모리의 영속성을 보장하는 설정이 아니다.

### 권장 보완

MVP에서는 Firestore에 최소 상태를 명시적으로 저장한다.

```text
threads/{thread_id}
  profile
  pending_question
  missing_slots
  last_intent
  updated_at
  expires_at
```

- 로컬 개발: `MemorySaver` 사용 가능
- Cloud Run: 요청 시작 시 Firestore 상태 로드, 응답 완료 후 저장
- 장기 대화 전체를 복원하기보다 절차 안내에 필요한 최소 프로필만 저장
- 구현 시간이 부족하면 LangGraph의 장기 interrupt 대신 다음 사용자 메시지를 새로운 요청으로 처리하고 Firestore의 `pending_question`을 참고
- TTL을 두어 오래된 상태를 자동 삭제

참고: [Cloud Run 최소 인스턴스 문서](https://docs.cloud.google.com/run/docs/configuring/min-instances)

---

## 3. 인용 검증 강화

### 현재 위험

현재 설계는 답변 문장의 `cite_id`가 실제 `evidence`에 존재하는지를 코드로 확인한다. 그러나 관련 없는 조문 ID를 붙인 잘못된 문장도 이 검사를 통과할 수 있다.

### 권장 답변 구조

```json
{
  "sentences": [
    {
      "text": "답변 문장",
      "cite_ids": ["RULE-001-15"],
      "supporting_quotes": ["근거가 되는 원문의 짧은 구간"]
    }
  ]
}
```

### 검증 규칙

- `cite_id`가 실제 검색 결과에 존재해야 한다.
- `supporting_quote`가 해당 근거 본문에 실제 포함되어야 한다.
- 날짜, 금액, 자격조건, 신청기한, 예외사항은 근거 본문에 동일 사실이 없으면 삭제한다.
- 근거가 없는 체크리스트 항목도 삭제한다.
- 모든 문장이 삭제되면 fallback으로 전환한다.
- 원문과 요약 사이의 의미 일치는 LLM 검증 또는 사람 평가로 별도 측정한다.

### 추가 평가 지표

| 지표 | 의미 |
|---|---|
| Citation Coverage | 답변 문장 중 인용이 붙은 비율 |
| Citation Precision | 인용이 실제 문장을 뒷받침하는 비율 |
| Faithfulness | 답변 내용이 근거 범위를 벗어나지 않는 비율 |
| Retrieval Recall@5 | 기대 조문이 검색 상위 5개에 포함된 비율 |

---

## 4. 모델·SDK·API 결정 고정

### 권장 결정 항목

개발 시작 시 다음 값을 한 곳의 설정으로 고정한다.

| 항목 | 결정 필요 내용 |
|---|---|
| 기본 모델 | 예: `gemini-3.5-flash` |
| 대체 모델 | 기본 모델 장애·쿼터 문제 시 사용할 모델 |
| API | Gemini API in Vertex AI |
| 위치 | 실제 지원 여부를 확인한 `GOOGLE_CLOUD_LOCATION` |
| 직접 SDK | `google-genai` |
| Agent 연결 | LangChain/LangGraph에서 사용하는 어댑터와 역할 |
| 구조화 출력 | Pydantic 스키마와 실패 시 재시도 횟수 |
| 버전 고정 | `pyproject.toml`과 `uv.lock` |

`gemini-3.5-flash`는 사용 가능한 GA 모델이다. 더 최신 모델을 선택할 경우에도 단순히 최신이라는 이유로 변경하지 말고 대표 질문 10개로 다음 항목을 비교한 뒤 결정한다.

- 정답률
- 구조화 출력 성공률
- 도구 호출 성공률
- p50·p90 응답시간
- 요청당 토큰과 예상 비용

참고:

- [Gemini 3.5 Flash](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/guides/gemini-3-5-flash)
- [Gemini 3.8 Flash](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/guides/gemini-3-8-flash)
- [Vertex AI Gemini API 빠른 시작](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/start/quickstart)

---

## 5. 보안·개인정보 처리

### 입력과 로그

- 화면에 "학번·성명·전화번호 등 개인정보를 입력하지 마세요"를 표시한다.
- 저장 전에 학번, 전화번호, 이메일 패턴을 마스킹한다.
- 대화 원문을 꼭 저장해야 하는지 구분하고, 통계에 필요한 최소 데이터만 남긴다.
- 로그 보존 기간과 삭제 방법을 명시한다.
- Firestore 문서는 서버 서비스 계정만 접근하도록 한다.

### 외부 콘텐츠

- 수집 URL을 승인된 대학 도메인 allowlist로 제한한다.
- 공지·규정·첨부파일의 문장은 명령이 아니라 데이터로만 취급한다.
- HTML의 스크립트, 이벤트 속성, 숨김 콘텐츠를 제거한다.
- 원문 링크도 허용된 도메인인지 검사한다.
- 도구가 임의 URL을 요청하지 못하도록 하여 SSRF를 방지한다.

### 비밀정보

- 관리자 키와 외부 API 비밀정보는 Secret Manager에서 주입한다.
- `.env`, 서비스 계정 키 파일, 원본 승인 문서는 Git에 넣지 않는다.
- 가능하면 키 파일 대신 Cloud Run 서비스 계정과 Application Default Credentials를 사용한다.

---

## 6. 평가 데이터 분리와 판정 기준

### 데이터 분리

- 개발셋 35문항: 검색·프롬프트 개선에 반복 사용
- 최종 평가셋 15문항: 구현 중 정답을 직접 보며 튜닝하지 않음
- 범위 밖 질문 10문항: fallback 평가 전용
- 위치 질문은 해당 기능을 구현한 경우에만 별도 지표로 계산

### 정답률 정의

문항별 판정을 단순 LLM 점수 하나로 두지 말고 다음처럼 분리한다.

1. 기대 조문 검색 성공
2. 핵심 사실 포함
3. 잘못된 사실 없음
4. 유효한 인용 포함
5. 필요한 다음 행동 또는 담당 부서 포함

LLM 채점은 1차 자동 평가로 사용하고, 오답과 경계 사례는 이동우가 수동으로 확정한다. 최종 보고서에는 자동 평가와 수동 확인 결과를 구분해 적는다.

---

## 7. 완료 조건과 기능 중단 기준

각 기능의 Definition of Done을 명시하면 일정 후반의 불필요한 확장을 막을 수 있다.

| 기능 | 완료 조건 |
|---|---|
| 규정 검색 | 대표 질문에서 기대 조문이 상위 5개에 포함됨 |
| 근거 답변 | 모든 사실 문장에 유효한 근거가 있고 없는 경우 fallback |
| 실시간 정보 | 캐시 적용, 타임아웃·파싱 실패 시 안내 문구 반환 |
| 조건 되묻기 | 부족한 조건을 한 번에 질문하고 다음 요청에서 이어서 처리 |
| 대시보드 | 미응답 질문과 부정 피드백을 조회 가능 |
| 위치 안내 | 부서 검색 결과와 지도 링크가 정확한 경우에만 완료 처리 |

권장 중단 기준:

- 4일차까지 규정 질문 E2E가 불안정하면 위치 기능을 제외한다.
- 5일차까지 정확도가 목표에서 크게 벗어나면 새 기능 개발을 중단한다.
- 7일차 오전 이후에는 기능 추가 없이 버그·문서·시연만 처리한다.

---

## 8. 시연·운영 Runbook

### 배포 후 자동 확인

- `/api/status` 응답
- Gemini 호출 1회
- 규정 검색 1회
- Firestore 읽기·쓰기
- 대표 E2E 질문 1회
- 범위 밖 질문 fallback 1회
- 원문 링크 접근 확인

### 시연 장애 대비

- 대표 시나리오 3개와 예상 답변 구조를 문서화한다.
- 외부 학교 사이트가 장애일 때 마지막 정상 캐시를 사용한다.
- 캐시 응답에는 마지막 확인 시각을 표시한다.
- SSE 실패 시 일반 JSON 응답으로 전환할 수 있게 한다.
- 정상 동작한 Cloud Run revision과 롤백 명령을 기록한다.
- 시연 직전 워밍업과 smoke test를 한 번에 실행하는 스크립트를 준비한다.

캐시·샘플 데이터로 시연할 경우 실제 조회처럼 오해되지 않도록 화면이나 발표에서 명확히 표시한다.

---

## 9. 비용·쿼터 통제

- GCP 예산 알림을 설정한다.
- Cloud Run `max-instances`를 제한한다.
- `min-instances=1`은 시연·심사 기간 중심으로 사용한다.
- Agent 도구 호출 최대 4회와 별도로 요청당 모델 호출 횟수 및 출력 토큰 상한을 둔다.
- 평가 실행의 동시성을 제한하고 429·5xx에 지수 백오프와 지터를 적용한다.
- 검색 색인 작업과 50문항 평가 전에 관련 API 쿼터를 확인한다.
- 일별 비용을 `ai-usage-log.md`에 함께 기록하면 완료보고서의 운영 가능성 근거로 사용할 수 있다.

참고:

- [Cloud Run 과금 설정](https://docs.cloud.google.com/run/docs/configuring/billing-settings)
- [Cloud Run 최소 인스턴스](https://docs.cloud.google.com/run/docs/configuring/min-instances)

---

## 10. 데이터 출처·변경 감지 보완

- 파일명 버전뿐 아니라 콘텐츠 SHA-256을 저장한다.
- 가능하면 HTTP `ETag`와 `Last-Modified`도 기록한다.
- 수집 시각, 원본 URL, 변환 도구와 버전, 변환 성공 여부를 manifest에 남긴다.
- 원문이 삭제되거나 파싱 결과가 비정상적으로 짧아지면 기존 색인을 즉시 덮어쓰지 않는다.
- 규정별 조 개수와 이전 버전 대비 증감률을 검사한다.
- 데이터 이용약관, 공개 범위, 승인 근거를 데이터 소스별로 기록한다.

권장 manifest 예시:

```json
{
  "source_url": "https://example.edu/rule/file.hwp",
  "fetched_at": "2026-09-29T09:00:00+09:00",
  "etag": "...",
  "content_hash": "sha256:...",
  "converter": "hwp5txt ...",
  "article_count": 42,
  "status": "indexed"
}
```

---

## 적용 우선순위

### P0 — 개발 시작 전에 구현전략에 반영

1. Search Spike와 정확한 전환 시각
2. Cloud Run 상태 영속화 방식
3. 인용 충실성 검증
4. 모델·SDK·API 고정

### P1 — 1~3일차에 반영

5. 개인정보 마스킹과 입력 안내
6. 평가셋 분리 및 정답률 판정표
7. 기능별 완료·중단 기준

### P2 — 배포 전 반영

8. 시연 Runbook과 smoke test
9. 비용·쿼터 제한
10. 데이터 manifest와 변경 감지

## 결론

새 기능을 더 추가하기보다는 P0 네 항목을 기존 구현전략에 포함하는 것이 우선이다. 이 보완은 현재 MVP 범위를 넓히지 않으면서도 검색 연동 실패, Cloud Run 재시작, 잘못된 인용, 모델·SDK 혼선으로 인한 일정 손실을 줄여 준다.

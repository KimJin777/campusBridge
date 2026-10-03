# 캠퍼스 브릿지 (campusBridge)

**기존 업무를 바꾸지 않고, 흩어진 학교 정보를 이어 주는 학생 안내 AI Agent**

학교 구성원은 지금 하던 일을 지금처럼 합니다. 학칙은 규정관리시스템에, 공지는 홈페이지 게시판에, 연락처는 전화번호부에 그대로 올립니다.
캠퍼스 브릿지는 그 결과물을 모아 학생의 질문에 **근거와 함께** 답합니다. 챗봇을 위해 자료를 새로 만들거나 부서 업무를 바꾸지 않습니다.

- 제4회 경남 AI·SW 경진대회 출품작 (일반부 · 교육·인재육성 분야)
- 개발 기간: 2026-09-29 ~ 2026-10-06
- 실증 대상: 경남대학교 공개 자료. 특정 학교에 묶이지 않도록 설계했습니다.
- 서비스: https://campusbridge-web-o3jnh7zl4a-du.a.run.app/

## 무엇을 하나

| 기능 | 설명 |
|---|---|
| 근거 인용형 규정 안내 | 학칙·규정을 조(條) 단위로 색인합니다. 답에는 조항·개정일·소관 부서·원문 링크를 붙입니다. 근거가 없으면 지어내지 않고 담당 부서를 안내합니다. |
| 실시간 정보 도구 | 학사·장학 공지(RSS), 학사일정, 주간 식단, 행사 일정을 도구로 조회합니다. |
| 맞춤 절차 체크리스트 | 학년·학적·장학 같은 조건을 되물은 뒤 해당 절차를 정리합니다. |
| 부서 연락처·교내 위치 | 전화번호부와 장소표를 분리해 관리하고, 위치는 텍스트로 안내합니다. |
| 답변 검증 | 답 문장마다 인용문과 숫자가 실제 근거에 있는지 확인하고, 통과하지 못한 문장은 내보내지 않습니다. |
| 미응답·제보 처리 | 답하지 못한 질문과 학생 제보를 모읍니다. 자동 처리 Job이 원인 분석, 자료 등록 제안, 재확인을 맡고, 공식 자료를 바꾸는 단계는 사람이 승인합니다. |
| 관리자 페이지 | 미응답 대시보드, 제보함, 교내 문서 등록, 용어 사전, 버전·변경 내역을 다룹니다. |

**서비스 범위**: 대학생이 학교생활에 필요한 정보만 다룹니다. 범위 밖 질문에는 답하지 않고 범위 안내로 끝냅니다.

## 동작 흐름

```
질문 ──▶ 분류(의도·필요 정보) ──▶ 도구 실행(규정 검색 · 공지 RSS · 일정 · 식단 · 전화번호부 · 장소)
                                        │
                                        ▼
            답변 ◀── 검증(인용·숫자 대조) ◀── 작성(근거 인용 필수)
              │
              └─ 근거 없음 / 검증 실패 → 담당 부서 안내 + 미응답 목록에 기록
```

- Agent는 LangGraph 그래프입니다(`backend/agent/graph.py`). 호출 횟수, 시간 예산, 도구 호출 수에 상한이 있습니다.
- 수집 Job이 규정 HWP, 홈페이지, 첨부 HWP·PDF를 읽어 조·절 단위로 쪼갠 뒤 검색 색인에 넣습니다(`backend/ingest/`).

## 기술 스택

- **Agent**: LangChain · LangGraph
- **모델**: Gemini (Vertex AI) — 주 모델과 대체 모델을 두고, 시간 초과나 한도 초과 시 대체 모델로 넘어갑니다
- **검색**: Vertex AI Search
- **백엔드**: Python 3.12 · FastAPI · Firestore · Cloud Storage
- **수집**: pyhwp(HWP), pypdf, feedparser(RSS), httpx · BeautifulSoup
- **프론트**: HTML · CSS · JavaScript (모바일 반응형, SSE 스트리밍)
- **배포·운영**: Docker · Cloud Run · Cloud Build · Cloud Scheduler (GCP)
- **평가**: pytest · 학생 관점 질문셋 자동 평가 · 매일 밤 평가 Job

## 폴더

```
backend/
  agent/     LangGraph 그래프·노드·프롬프트·검증(verify)
  app/       FastAPI 앱(main), 설정, 외부 호출 래퍼, 버전·변경 내역, 밤 평가·자동 처리
  domain/    공통 계약(근거·대화·답변·오류)
  ingest/    규정 HWP·홈페이지·첨부 수집과 색인 Job
  tools/     공지·일정·식단·전화번호부·장소 도구
  admin/     관리자 API
  reports/   학생 제보·꿀팁 처리
  store/     대화 상태·멱등 처리
frontend/    학생 화면(index), 관리자 화면(admin), 일정·꿀팁 화면
config/      수집 대상(sources.yaml), 용어 사전(glossary.yml)
eval/        평가 실행기와 질문셋
scripts/     배포 후 점검(smoke.sh) 등
```

## 로컬 실행

```bash
uv sync                      # 의존성 설치 (수집까지 하려면: uv sync --extra ingest)
uv run pytest                # 테스트
uv run uvicorn backend.app.main:app --reload --port 8080
```

실행하려면 GCP 프로젝트(Vertex AI, Vertex AI Search, Firestore)가 있어야 합니다. 주요 환경 변수는 아래와 같습니다. 전체 목록과 기본값은 `backend/app/config.py`에 있습니다.

| 변수 | 용도 |
|---|---|
| `GCP_PROJECT_ID` | GCP 프로젝트 |
| `FIRESTORE_DB` | Firestore 데이터베이스 이름 (기본 `campusbridge`) |
| `RULES_BUCKET` | 원본·가공 자료 버킷 |
| `SEARCH_DATASTORE_ID`, `SEARCH_SERVING_CONFIG` | Vertex AI Search |
| `GEMINI_MODEL`, `GEMINI_FALLBACK_MODEL` | 주 모델·대체 모델 |
| `ADMIN_EMAILS`, `GOOGLE_OAUTH_CLIENT_ID` | 관리자 로그인 |

비밀값은 저장소에 넣지 않고 Secret Manager나 실행 환경 변수로 넣습니다.

## 배포

`cloudbuild.yaml`이 웹·수집 이미지를 빌드해 Cloud Run 서비스와 Job으로 배포합니다. 배포 뒤에는 `scripts/smoke.sh <URL> <버전>`으로 점검합니다.
버전과 변경 내역은 `backend/app/version.py` 한 곳에서 관리합니다.

## 데이터와 개인정보

- 공개 자료(규정관리시스템, 홈페이지 게시판·안내 페이지)와, 학교가 공개 답변 인용을 승인한 교내 문서만 씁니다.
- 원본 HWP·PDF와 검수 전 업로드는 저장소에 넣지 않습니다(`data/raw/`, `data/staging/`는 git 제외).
- 전화번호부는 부서·조직 단위로만 색인하고, 교직원 개인 이름은 저장하지 않습니다.
- 미응답 질문처럼 저장하는 질문은 개인정보 패턴을 가린 뒤 저장합니다. 처리 방침: `frontend/privacy.html`

## 이 프로젝트를 만든 방식

설계, 코딩, 리뷰는 사람 한 명과 AI 에이전트 여러 개(Claude, Gemini, Codex)가 공용 게시판에서 나눠 맡았습니다.
에이전트끼리 협업한 규칙과 도구(게시판, 폴링, 위임 규정)는 개발 기간 전에 만들어 둔 별도 비공개 프로젝트(prototype)의 기존 자산이며, 이 저장소의 코드는 개발 기간에 새로 작성했습니다.

"""모든 환경값의 단일 진입점(상세설계 00 공통 규약, 07 §4).

코드에 프로젝트 ID·모델명·URL을 하드코딩하지 않는다. 운영 값은 Cloud Build 치환 변수로 주입한다.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _list(name: str, default: str = "") -> tuple[str, ...]:
    raw = os.environ.get(name, default)
    # 쉼표 또는 | 구분(Windows gcloud.cmd에서 치환값 쉼표가 깨지는 문제 회피)
    return tuple(x.strip() for x in re.split(r"[,|]", raw) if x.strip())


# 서비스 범위 규칙(교수님 2026-09-29): 대학생에게 필요한 정보만 — 범위 밖 예시는 화면에 두지 않는다
DEFAULT_SUGGESTIONS = (
    "휴학 신청 절차 알려 주세요",
    "성적 경고 기준과 재수강 규정",
    "오늘 학생식당 메뉴",
    "장학금 신청 언제까지예요?",
    "학사관리팀 어디 있어요?",  # 전화번호부 기준 실제 부서명(학사지원팀 없음)
)


def _version() -> str:
    from backend.app.version import VERSION

    return VERSION


@dataclass(frozen=True)
class Settings:
    # GCP
    gcp_project_id: str = field(default_factory=lambda: _str("GCP_PROJECT_ID"))
    firestore_db: str = field(default_factory=lambda: _str("FIRESTORE_DB", "campusbridge"))
    rules_bucket: str = field(default_factory=lambda: _str("RULES_BUCKET"))

    # 변경분 강제 재수집 Job(04 §6, #535 결정) — 비어 있으면 관리자 API가 안전 거부
    ingestion_job_name: str = field(default_factory=lambda: _str("INGESTION_JOB_NAME"))
    ingestion_job_location: str = field(
        default_factory=lambda: _str("INGESTION_JOB_LOCATION", "asia-northeast3")
    )

    # Gemini (D4 2026-09-29, 교수님 #553: 3.5 계열 유지 — 대체는 3.5-flash-lite)
    gemini_location: str = field(default_factory=lambda: _str("GEMINI_LOCATION", "global"))
    gemini_model: str = field(default_factory=lambda: _str("GEMINI_MODEL", "gemini-3.5-flash"))
    gemini_fallback_model: str = field(
        default_factory=lambda: _str("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite")
    )

    # classify·act의 thinking 수준(3.x 모델). 2026-09-29 실측: low ≈ 2.2초 안정, 기본값 ≈ 4.6초
    classify_thinking: str = field(default_factory=lambda: _str("GEMINI_CLASSIFY_THINKING", "low"))
    # compose도 low: 실측 기본값 9.2초·인용 누락 ↔ low 2.7초·인용 정상(2026-09-29, 실제 규정 20조)
    compose_thinking: str = field(default_factory=lambda: _str("GEMINI_COMPOSE_THINKING", "low"))

    # Vertex AI Search
    search_location: str = field(default_factory=lambda: _str("SEARCH_LOCATION", "global"))
    search_datastore_id: str = field(
        default_factory=lambda: _str("SEARCH_DATASTORE_ID", "rules-articles")
    )
    search_serving_config: str = field(default_factory=lambda: _str("SEARCH_SERVING_CONFIG"))

    # 관리자 인증(04 §6) — 허용 이메일은 소문자로 비교
    admin_emails: frozenset[str] = field(
        default_factory=lambda: frozenset(e.lower() for e in _list("ADMIN_EMAILS"))
    )
    google_oauth_client_id: str = field(default_factory=lambda: _str("GOOGLE_OAUTH_CLIENT_ID"))
    # 카카오 지도 JavaScript 키(웹 페이지에 공개되는 키, 카카오 콘솔의 등록 도메인에서만 동작)
    kakao_js_key: str = field(default_factory=lambda: _str("KAKAO_JS_KEY"))

    # 외부 수집(03 공통, 01 §0)
    allowed_hosts: frozenset[str] = field(
        default_factory=lambda: frozenset(
            _list("ALLOWED_HOSTS", "yz.kyungnam.ac.kr,www.kyungnam.ac.kr")
        )
    )
    rss_academic: str = field(
        default_factory=lambda: _str("RSS_ACADEMIC", "/bbs/ko/1398/rssList.do?row=50")
    )
    rss_scholarship: str = field(
        default_factory=lambda: _str("RSS_SCHOLARSHIP", "/bbs/ko/1407/rssList.do?row=50")
    )
    rss_general: str = field(
        default_factory=lambda: _str("RSS_GENERAL", "/bbs/ko/1408/rssList.do?row=50")
    )
    rss_events: str = field(  # 행사/세미나 게시판(교내 행사 일정 — 교수님 2026-09-29)
        default_factory=lambda: _str("RSS_EVENTS", "/bbs/ko/1129/rssList.do?row=50")
    )
    collection_mode: str = field(default_factory=lambda: _str("COLLECTION_MODE", "approved"))
    # 관리자 홈페이지 등록 수집(교수님 #688). robots.txt 결정에 따라 WEB_PAGES_ENABLED=0으로 끈다
    web_pages_enabled: bool = field(
        default_factory=lambda: _str("WEB_PAGES_ENABLED", "1").lower() not in ("0", "false", "off")
    )

    # 비용·시간 상한(02 §3, 07 §5)
    max_tool_calls: int = field(default_factory=lambda: _int("MAX_TOOL_CALLS", 4))
    max_llm_calls: int = field(default_factory=lambda: _int("MAX_LLM_CALLS", 6))
    max_output_tokens: int = field(default_factory=lambda: _int("MAX_OUTPUT_TOKENS", 2048))
    soft_budget_ms: int = field(default_factory=lambda: _int("SOFT_BUDGET_MS", 10000))
    hard_deadline_ms: int = field(default_factory=lambda: _int("HARD_DEADLINE_MS", 25000))

    # 버전은 backend/app/version.py가 단일 원본(환경값 APP_VERSION은 비상용 덮어쓰기)
    app_version: str = field(default_factory=lambda: _str("APP_VERSION") or _version())

    # 추천 질문 칩(05 §1-3) — 시연 전 문구 교체용. "|"로 구분
    suggestions: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            x.strip()
            for x in _str("SUGGESTIONS", "|".join(DEFAULT_SUGGESTIONS)).split("|")
            if x.strip()
        )
    )

    def is_admin(self, email: str | None) -> bool:
        return bool(email) and email.lower() in self.admin_emails


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

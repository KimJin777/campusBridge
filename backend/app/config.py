"""모든 환경값의 단일 진입점(상세설계 00 공통 규약, 07 §4).

코드에 프로젝트 ID·모델명·URL을 하드코딩하지 않는다. 운영 값은 Cloud Build 치환 변수로 주입한다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _list(name: str, default: str = "") -> tuple[str, ...]:
    raw = os.environ.get(name, default)
    return tuple(x.strip() for x in raw.split(",") if x.strip())


DEFAULT_SUGGESTIONS = (
    "휴학 신청 절차 알려 주세요",
    "성적 경고 기준과 재수강 규정",
    "오늘 학생식당 메뉴",
    "장학금 신청 언제까지예요?",
    "학사지원팀 어디 있어요?",
    "오늘 주식 뭐 사면 좋을까요?",
)


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

    # Gemini (D4 확인 2026-09-29: global에서 3.5-flash·2.5-flash 호출 OK)
    gemini_location: str = field(default_factory=lambda: _str("GEMINI_LOCATION", "global"))
    gemini_model: str = field(default_factory=lambda: _str("GEMINI_MODEL", "gemini-3.5-flash"))
    gemini_fallback_model: str = field(
        default_factory=lambda: _str("GEMINI_FALLBACK_MODEL", "gemini-2.5-flash")
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
    collection_mode: str = field(default_factory=lambda: _str("COLLECTION_MODE", "approved"))

    # 비용·시간 상한(02 §3, 07 §5)
    max_tool_calls: int = field(default_factory=lambda: _int("MAX_TOOL_CALLS", 4))
    max_llm_calls: int = field(default_factory=lambda: _int("MAX_LLM_CALLS", 6))
    max_output_tokens: int = field(default_factory=lambda: _int("MAX_OUTPUT_TOKENS", 2048))
    soft_budget_ms: int = field(default_factory=lambda: _int("SOFT_BUDGET_MS", 10000))
    hard_deadline_ms: int = field(default_factory=lambda: _int("HARD_DEADLINE_MS", 25000))

    app_version: str = field(default_factory=lambda: _str("APP_VERSION", "0.3.0"))

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

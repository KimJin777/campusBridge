"""첫 화면 '자주 묻는 질문' — 시기(월)별로 나눠 제시(교수님 #768·#769).

수강신청 무렵엔 수강신청, 학기 중엔 식단·통학버스, 방학 직전엔 계절학기처럼
그달에 많이 묻는 분류를 먼저 보여 주고, '전체 보기'에서 모든 분류를 보여 준다.
모두 우리 서비스가 원문 근거로 답할 수 있는 학사·생활 질문만 넣는다(범위 밖 예시 금지).
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

KST = timezone(timedelta(hours=9), "KST")
MAX_CHIPS = 6
ALWAYS = "통학버스(스쿨버스) 타는 곳이 어디예요?"  # 교수님 #768: 자주 묻는 질문에 통학버스

# (분류, 많이 묻는 달, 질문) — 달은 학사 일정 기준 대략값
CATALOG: tuple[tuple[str, tuple[int, ...], tuple[str, ...]], ...] = (
    (
        "수강신청",
        (1, 2, 3, 7, 8, 9),
        (
            "수강신청 기간 언제예요?",
            "수강 정정 기간 알려 주세요",
            "한 학기 최대 몇 학점까지 들을 수 있어요?",
        ),
    ),
    (
        "휴학·복학",
        (1, 2, 3, 7, 8, 9),
        ("휴학 신청 절차 알려 주세요", "복학 신청 언제까지예요?", "군휴학 신청 방법"),
    ),
    (
        "장학금",
        (1, 2, 3, 7, 8, 9),
        ("장학금 신청 언제까지예요?", "국가장학금과 교내장학금 중복으로 받을 수 있어요?"),
    ),
    (
        "학교생활",
        (3, 4, 5, 6, 9, 10, 11, 12),
        ("오늘 학생식당 메뉴", ALWAYS, "학사관리팀 어디 있어요?"),
    ),
    (
        "시험·성적",
        (4, 6, 10, 12, 1, 7),
        ("중간고사 기간 언제예요?", "성적 경고 기준과 재수강 규정", "성적 이의신청 방법"),
    ),
    (
        "계절학기",
        (5, 6, 11, 12),
        ("계절학기 신청 기간 언제예요?", "계절학기 최대 몇 학점까지 들을 수 있어요?"),
    ),
    (
        "졸업",
        (4, 5, 10, 11, 12),
        ("졸업 요건 알려 주세요", "조기졸업 조건이 뭐예요?"),
    ),
)


def _default_chips(m: int) -> list[str]:
    """기본 추천: 이달 분류를 돌아가며 한 개씩(최대 6개), 통학버스 항상 포함."""
    hot = [qs for _, months, qs in CATALOG if m in months]
    chips: list[str] = []
    for i in range(3):  # 한 분류가 칩을 독차지하지 않게
        for qs in hot:
            if i < len(qs) and qs[i] not in chips:
                chips.append(qs[i])
    chips = chips[: MAX_CHIPS - 1] if ALWAYS not in chips[:MAX_CHIPS] else chips[:MAX_CHIPS]
    if ALWAYS not in chips:
        chips.append(ALWAYS)
    return chips


def default_config() -> dict[str, Any]:
    """관리자가 아직 손대지 않았을 때의 목록(질문 풀 + 월별 목록)."""
    questions, ids = [], {}
    for label, _, qs in CATALOG:
        for q in qs:
            if q not in ids:
                ids[q] = f"q{len(ids) + 1}"
                questions.append({"id": ids[q], "text": q, "category": label})
    months = {str(m): [ids[q] for q in _default_chips(m)] for m in range(1, 13)}
    return {"questions": questions, "months": months}


MAX_QUESTIONS = 200
MAX_TEXT = 80
MAX_PER_MONTH = 12


def validate_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """관리자가 저장한 목록 검사·정리(교수님 #781). 문제가 있으면 ValueError."""
    qs = cfg.get("questions") or []
    if not isinstance(qs, list) or len(qs) > MAX_QUESTIONS:
        raise ValueError(f"질문은 {MAX_QUESTIONS}개까지입니다.")
    out, seen_id, seen_text = [], set(), set()
    for q in qs:
        qid = str(q.get("id") or "")[:40]
        text = " ".join(str(q.get("text") or "").split())[:MAX_TEXT]
        cat = " ".join(str(q.get("category") or "기타").split())[:20] or "기타"
        if not qid or not text or qid in seen_id:
            raise ValueError("질문 번호·내용이 비었거나 겹칩니다.")
        if text in seen_text:
            raise ValueError(f"같은 질문이 두 번 있습니다: {text}")
        seen_id.add(qid)
        seen_text.add(text)
        out.append({"id": qid, "text": text, "category": cat})
    months = {}
    for m in range(1, 13):
        ids = [str(x) for x in (cfg.get("months") or {}).get(str(m), [])]
        ids = [x for i, x in enumerate(ids) if x in seen_id and x not in ids[:i]]
        if len(ids) > MAX_PER_MONTH:
            raise ValueError(f"{m}월 목록은 {MAX_PER_MONTH}개까지입니다.")
        months[str(m)] = ids
    return {"questions": out, "months": months}


def view(cfg: dict[str, Any], m: int) -> dict[str, Any]:
    """이달 칩(월 목록 순서, 최대 6개)과 전체 분류(이달 분류를 위로)."""
    by_id = {q["id"]: q for q in cfg["questions"]}
    month_ids = [x for x in cfg["months"].get(str(m), []) if x in by_id]
    chips = [by_id[x]["text"] for x in month_ids][:MAX_CHIPS]
    hot_cats = {by_id[x]["category"] for x in month_ids}
    groups: dict[str, list[str]] = {}
    for q in cfg["questions"]:
        groups.setdefault(q["category"], []).append(q["text"])
    out = [{"label": c, "hot": c in hot_cats, "items": qs} for c, qs in groups.items()]
    out.sort(key=lambda g: not g["hot"])
    return {"month": m, "items": chips, "groups": out}


def monthly(today: date) -> dict[str, Any]:
    return view(default_config(), today.month)


_state: dict[str, Any] = {"at": float("-inf"), "cfg": None}
_client: Any = None
TTL_SECONDS = 300


async def current(settings: Any) -> dict[str, Any]:
    """관리자가 저장한 목록(없거나 장애면 기본값). 5분 캐시, 실패는 캐시하지 않는다."""
    global _client
    if _state["cfg"] is not None and time.monotonic() - _state["at"] < TTL_SECONDS:
        return _state["cfg"]
    if not settings.gcp_project_id:
        return default_config()
    try:
        if _client is None:
            from google.cloud import firestore

            _client = firestore.AsyncClient(
                project=settings.gcp_project_id, database=settings.firestore_db
            )
        snap = await _client.collection("site_config").document("faq").get()
        cfg = validate_config(snap.to_dict() or {}) if snap.exists else default_config()
    except Exception:  # noqa: BLE001
        return _state["cfg"] or default_config()
    _state.update(at=time.monotonic(), cfg=cfg)
    return cfg


def invalidate() -> None:
    _state["at"] = float("-inf")


async def this_month(settings: Any) -> dict[str, Any]:
    return view(await current(settings), datetime.now(KST).month)

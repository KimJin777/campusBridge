# ruff: noqa: E501 — 안내 문구·지시문은 줄바꿈하지 않는다
"""제보함 에이전트 조치안(교수님 #786·CLI "1번 전부", GPT5 #792 기준).

에이전트가 스스로 하는 일은 '분석·원문 재비교·근거 고정 초안·재검증·안전한 상태 전이'까지다.
학생 답변에 쓰이는 자료(색인·장소표)는 관리자가 [승인·반영]을 눌러야 바뀐다.

- ① 원문이 낡음: 인용한 학교 페이지를 지금 다시 읽어 당시 인용문이 아직 있는지 비교 → 재수집 제안
- ② 장소가 없음·틀림: 제보 글은 검색어로만 쓰고, 위치·전화는 학교 원문 인용에서만 채운 장소 초안
- ③ 질문과 어긋난 답: 미응답 목록에 연결(자료 보강 뒤 재확인)
- 해결 판정: 자료가 실제로 바뀐 뒤, 원 질문 + 바꿔 말한 질문을 새로 실행해 2/2 통과해야 종결.
  장소 제보면 답변에 승인한 위치가 실제로 들어 있어야 한다(결정적 검사, LLM 판정 하나로 닫지 않음).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from backend.tools.common import is_school_url

PLACE_Q = re.compile(r"어디|위치|몇\s*층|찾아가|가는\s*길|호실|사무실|타는\s*곳")
DRAFT_TTL = timedelta(hours=24)  # 초안이 이보다 오래되면 다시 만든다(GPT5 #792)


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


URL_RE = re.compile(r"https?://[^\s<>\"'）)\]]+")


def school_urls(*texts: str) -> list[str]:
    """제보 글·질문 속 학교 도메인 주소(교수님 #919: 제보자가 준 출처를 쓴다). 최대 3개."""
    out: list[str] = []
    for text in texts:
        for raw in URL_RE.findall(text or ""):
            url = raw.rstrip(".,;:!?")
            if is_school_url(url) and url not in out:
                out.append(url)
    return out[:3]


def fix_type(report: dict[str, Any]) -> str:
    """제보를 어느 조치로 다룰지(결정적). register > place > recollect > quality > unanswered.

    - register: 제보에 학교 주소가 있으면 그 페이지를 등록해 수집하는 조치(#919 P1)
    - quality: 자료는 있는데 답이 어긋나거나 형식이 문제(#919 P2) — 평가셋에 쌓고 재확인
    """
    question = (report.get("snapshot") or {}).get("question_masked") or ""
    if school_urls(report.get("text_masked") or "", question):
        return "register"
    if PLACE_Q.search(question):
        return "place"
    if report.get("kind") == "stale_source":
        return "recollect"
    if report.get("kind") == "answer_quality":
        return "quality"
    return "unanswered"


def source_of(evidence_id: str) -> str | None:
    """인용 근거 ID → 재수집할 수집 원천(수집 Job source_ids)."""
    eid = evidence_id or ""
    if eid.startswith("guide:web-"):
        return "web_pages"
    if eid.startswith(("guide:notice-", "notice:", "cal:")):
        return "events"
    if eid.startswith("guide:"):
        return "academic_guides"
    if eid.startswith("menu:"):
        return "menus"
    if eid.startswith(("place:", "dept:", "doc:")):
        return None  # 장소표·교내 문서는 재수집 대상이 아님(관리자가 직접)
    return "rules" if re.match(r"^\d+_", eid) else None


def quote_present(page_text: str, quote: str) -> bool:
    """당시 인용문이 지금 원문에도 있는가. 인용문을 30자 조각으로 나눠 2/3 이상 있으면 같다고 본다."""
    page, q = _norm(page_text), _norm(quote)
    if not q:
        return True
    chunks = [q[i : i + 30] for i in range(0, len(q), 30) if len(q[i : i + 30]) >= 10][:6]
    if not chunks:
        return q in page
    return sum(c in page for c in chunks) * 3 >= len(chunks) * 2


async def compare_sources(
    snapshot: dict[str, Any], fetch_text: Callable[[str], Awaitable[str]]
) -> list[dict[str, Any]]:
    """① 인용한 원문 페이지마다 same / changed / unreachable."""
    quotes = snapshot.get("cited_quotes") or {}
    out = []
    for card in snapshot.get("cards") or []:
        cid, url = str(card.get("id") or ""), str(card.get("url") or "")
        row = {"id": cid, "title": card.get("title"), "url": url, "source": source_of(cid)}
        if not url or not is_school_url(url):
            out.append({**row, "status": "unreachable"})
            continue
        try:
            text = await fetch_text(url)
        except Exception:  # noqa: BLE001 — 한 페이지 실패가 전체를 막지 않게
            out.append({**row, "status": "unreachable"})
            continue
        same = quote_present(text, quotes.get(cid, ""))
        out.append({**row, "status": "same" if same else "changed"})
    return out


# ── ② 장소 초안 ─────────────────────────────────────────────────────────
PLACE_WORD = re.compile(
    r"(은|는|이|가|을|를)?\s*(어디(에|로)?|위치(가|는)?|몇\s*층|찾아가(는|려면)?|가는\s*길)"
    r".*$"
)


def place_name(question: str) -> str:
    """'반도체부트캠프사업단이 어디있나요?' → '반도체부트캠프사업단'."""
    q = re.sub(r"[?？!.]", " ", question or "").strip()
    return PLACE_WORD.sub("", q).strip()[:40] or q[:40]


class PlaceDraftOut(BaseModel):
    found: bool = Field(description="근거 안에 그 장소·부서의 위치가 있으면 true")
    name: str = Field(default="", description="장소·부서 이름(근거에 적힌 그대로)")
    location: str | None = Field(
        default=None, description="건물·층·호실(근거 인용문에 있는 그대로)"
    )
    phone: str | None = Field(default=None, description="대표 전화(근거 인용문에 있는 그대로)")
    evidence_id: str = Field(default="", description="위치를 찾은 근거의 id")
    quote: str = Field(default="", description="위치가 적힌 근거 문장을 글자 그대로 복사")


PLACE_PROMPT = (
    "대학 캠퍼스 장소표 초안 작성자다. [근거]는 학교 공식 원문이다. 질문한 장소·부서의 위치(건물·층·호실)와 "
    "대표 전화를 [근거]에서만 찾아라. 근거에 없으면 found=false. 값을 지어내거나 추측하지 말고, quote에는 "
    "위치가 적힌 근거 문장을 글자 그대로 복사하라."
)


def check_draft(
    out: PlaceDraftOut, evidence: dict[str, dict[str, Any]], report_text: str
) -> str | None:
    """초안 검증(GPT5 #792). 문제가 있으면 사유, 없으면 None."""
    if not out.found or not out.location:
        return "학교 원문에서 위치를 찾지 못했습니다"
    ev = evidence.get(out.evidence_id)
    if ev is None or not is_school_url(str(ev.get("url") or "")):
        return "근거가 학교 원문이 아닙니다"
    quote = _norm(out.quote)
    if len(quote) < 6 or quote not in _norm(ev["text"]):
        return "인용문이 원문에 없습니다"
    if _norm(out.location) not in quote:
        return "위치가 인용문에 없습니다"
    if out.phone and re.sub(r"\D", "", out.phone) not in re.sub(r"\D", "", out.quote):
        return "전화번호가 인용문에 없습니다"
    if _norm(out.location) in _norm(report_text) and _norm(out.location) not in _norm(ev["text"]):
        return "제보 문구를 그대로 옮긴 값입니다"
    return None


async def draft_place(
    question: str,
    report_text: str,
    search: Callable[[str], Awaitable[list[dict[str, Any]]]],
    extract: Callable[[str], Awaitable[PlaceDraftOut]],
) -> dict[str, Any]:
    """제보 글은 검색어로만(프롬프트에 넣지 않는다). 초안 또는 실패 사유."""
    name = place_name(question)
    hits = await search(f"{name} 위치 {report_text[:40]}".strip())
    evidence = {e["id"]: e for e in hits if is_school_url(str(e.get("url") or ""))}
    if not evidence:
        return {"ok": False, "reason": "학교 원문에서 관련 자료를 찾지 못했습니다", "name": name}
    blocks = "\n\n".join(
        f"[근거 id={e['id']}] {e.get('title') or ''}\n{str(e['text'])[:1500]}"
        for e in list(evidence.values())[:6]
    )
    out = await extract(f"[질문한 장소·부서]\n{name}\n\n[근거]\n{blocks}")
    if out.phone and re.sub(r"\D", "", out.phone) not in re.sub(r"\D", "", out.quote):
        out = out.model_copy(update={"phone": None})  # 인용문에 없는 전화는 빼고 위치만 살린다
    problem = check_draft(out, evidence, report_text)
    if problem:
        return {"ok": False, "reason": problem, "name": name}
    ev = evidence[out.evidence_id]
    return {
        "ok": True,
        "name": out.name or name,
        "location": out.location,
        "phone": out.phone
        if out.phone and re.fullmatch(r"0\d{1,2}-\d{3,4}-\d{4}", out.phone)
        else None,
        "quote": out.quote,
        "source_url": ev["url"],
        "source_title": ev.get("title"),
        "content_hash": hashlib.sha256(str(ev["text"]).encode()).hexdigest()[:16],
        "drafted_at": datetime.now(UTC),
    }


def place_id_for(name: str) -> str:
    return "rpt-" + hashlib.sha1(_norm(name).encode()).hexdigest()[:12]


# ── 해결 판정(2/2) ──────────────────────────────────────────────────────
def answer_text(final: dict[str, Any]) -> str:
    ans = final.get("answer")
    return " ".join(s.text for s in getattr(ans, "sentences", []) or [])


# 답은 했지만 실제로는 '못 찾았다'는 답(실운영 테스트에서 통과로 잘못 본 사례, 2026-10-01)
NOT_FOUND = re.compile(
    r"찾지\s*못|확인(할|되)\s*수\s*없|확인되지\s*않|정보가\s*없|내용이\s*없|안내되어\s*있지\s*않|알\s*수\s*없|나와\s*있지\s*않"
)


def same_as_reported(new: str, reported: str) -> bool:
    """제보된(틀렸다는) 답과 사실상 같은 답인가 — 같은 문장을 다시 내면 해결이 아니다."""
    if len(_norm(reported)) < 10:
        return False
    return quote_present(new, reported) or quote_present(reported, new)


def doc_prefix(evidence_id: str) -> str:
    """같은 문서의 근거 ID 앞부분: 'guide:web-abc:3'→'guide:web-abc:', '196_main_30'→'196_'."""
    eid = evidence_id or ""
    if re.match(r"^\d+_", eid):
        return eid.split("_", 1)[0] + "_"
    return eid.rsplit(":", 1)[0] + ":" if eid.count(":") >= 2 else eid


def run_ok(
    final: dict[str, Any],
    must_contain: str | None,
    reported: str = "",
    required: list[str] | None = None,
) -> tuple[bool, str]:
    if final.get("outcome") != "answer" or not final.get("answer"):
        return False, str(final.get("fallback_reason") or final.get("outcome") or "no_answer")
    ans = final["answer"]
    if not getattr(ans, "cited", None):
        return False, "인용 없음"
    if any(n.code == "source_conflict" for n in getattr(ans, "notices", []) or []):
        return False, "원문 충돌"
    if NOT_FOUND.search(answer_text(final)):
        return False, "답변이 '찾지 못함'"
    if reported and same_as_reported(answer_text(final), reported):
        return False, "제보된 답과 같음"
    if required and not any(str(c).startswith(p) for c in ans.cited for p in required):
        return (
            False,
            "바꾼 자료를 인용하지 않음",
        )  # 다른 옛 자료로 그럴듯하게 답한 경우(GPT5 #799-1)
    if must_contain and _norm(must_contain) not in _norm(answer_text(final)):
        return False, "승인한 위치가 답변에 없음"
    return True, "ok"


async def verify_fix(
    question: str,
    run_turn: Callable[[str], Awaitable[dict[str, Any]]],
    paraphrase: Callable[[str], Awaitable[str | None]],
    must_contain: str | None = None,
    reported: str = "",
    required: list[str] | None = None,
) -> dict[str, Any]:
    """원 질문 + 바꿔 말한 질문 2개를 새로 실행해 모두 통과해야 passed(GPT5 #792)."""
    other = None
    try:
        other = await paraphrase(question)
    except Exception:  # noqa: BLE001
        other = None
    if not other or _norm(other) == _norm(question):
        other = f"{question.rstrip('?？ ')} 알려 주세요"
    runs = []
    for q in (question, other):
        final = await run_turn(q)
        ok, why = run_ok(final, must_contain, reported, required)
        runs.append({"query": q, "ok": ok, "reason": why, "answer": answer_text(final)[:200]})
    return {"passed": all(r["ok"] for r in runs), "runs": runs, "checked_at": datetime.now(UTC)}


class Paraphrase(BaseModel):
    text: str = Field(description="같은 뜻의 다른 표현 한 문장")


PARAPHRASE_PROMPT = (
    "학생 질문을 뜻은 그대로 두고 다른 말로 한 문장 바꿔 써라. 새 정보를 더하지 마라."
)

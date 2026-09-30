# ruff: noqa: E501 — 판독 프롬프트·설명 문장은 줄바꿈하지 않는다
"""학생식당·푸드코트 주간 식단 PDF → 날짜·끼니별 메뉴 DB(campus_menus) (교수님 2026-09-30 #634).

"원문을 보라"는 안내가 무성의하다는 지적으로, 매일 수집 때 학교 식단 페이지에 올라온 최근 주간 식단표
PDF를 읽어 날짜별로 저장하고, 식단 질문은 DB에서 답한다.

PDF 판독: 글자 조각의 x·y 좌표를 쓴다(pypdf visitor). 머리행의 날짜 칸("9월 28일(월)")으로 요일 열을
정하고, 왼쪽 '구분' 열의 끼니 이름(조식·중식 차림·다올·석식·기숙사식·샐러드 등)에 가장 가까운 행으로
묶는다. 표 모양이 바뀌면 날짜 칸을 못 찾으므로 빈 결과(= 기존처럼 원문 링크 안내)로 끝난다.
"""

from __future__ import annotations

import io
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

from pydantic import BaseModel, Field

DAY_RE = re.compile(r"^(\d{1,2})월\s*(\d{1,2})일")
PLACEHOLDERS = {"♥", "-", "·"}
LABEL_MAX_X_RATIO = 0.8  # 첫 날짜 칸 x의 80%보다 왼쪽이면 '구분' 열 글자


@dataclass
class DayMenu:
    day: date
    sections: list[dict[str, Any]] = field(default_factory=list)  # [{name, items}]


def _fragments(pdf: bytes) -> list[tuple[float, float, str]]:
    from pypdf import PdfReader

    out: list[tuple[float, float, str]] = []
    for page in PdfReader(io.BytesIO(pdf)).pages:

        def visit(text, cm, tm, font, size, _out=out):
            t = (text or "").strip()
            if t:
                x = tm[4] * cm[0] + cm[4]
                y = tm[5] * cm[3] + cm[5]
                _out.append((x, y, t))

        page.extract_text(visitor_text=visit)
        break  # 주간 식단표는 1쪽
    return out


def _year_for(month: int, reference: date) -> int:
    if month - reference.month <= -6:
        return reference.year + 1
    if month - reference.month >= 6:
        return reference.year - 1
    return reference.year


class MenuSection(BaseModel):
    name: str = Field(
        description="끼니·코너 이름(예: 조식 천원의 아침밥, 중식 차림(정식), 석식 기숙사식)"
    )
    row_ys: list[int] = Field(description="이 구획에 속한 메뉴 행의 y 값들")


class MenuLayout(BaseModel):
    sections: list[MenuSection]


LAYOUT_PROMPT = (
    "대학 식당 주간 식단표의 표 구조를 판독한다. 왼쪽 '구분' 열 글자(좌표 포함)와 메뉴 행(y 값)을 보고, "
    "각 메뉴 행이 어느 끼니·코너 구획에 속하는지 묶어라. 구분 글자는 보통 자기 구획의 세로 가운데에 있다. "
    "바깥 구분(예: 여러 코너를 감싸는 '중식')은 안쪽 코너 이름 앞에 붙여라(예: '중식 차림(정식)'). "
    "모든 메뉴 행 y를 정확히 한 번씩만 배정하라."
)


def _nearest_layout(row_ys: list[int], labels: list[tuple[float, float, str]]) -> MenuLayout:
    """AI 판독 실패 시 대안: 가장 가까운 구분 글자로 묶는다(경계 행이 틀릴 수 있음)."""
    if not labels:
        return MenuLayout(sections=[MenuSection(name="식단", row_ys=row_ys)])
    out: dict[str, list[int]] = {}
    for y in row_ys:
        name = min(labels, key=lambda f: abs(f[1] - y))[2]
        out.setdefault(name, []).append(y)
    return MenuLayout(sections=[MenuSection(name=k, row_ys=v) for k, v in out.items()])


METHOD_RANK = {"layout": 3, "ocr": 2, "nearest": 1}
LAYOUT_ATTEMPTS = 2


def parse_week(
    pdf: bytes,
    reference: date,
    layout_llm: Callable[[str], MenuLayout | None] | None = None,
) -> list[DayMenu]:
    return parse_week_method(pdf, reference, layout_llm)[0]


def parse_week_method(
    pdf: bytes,
    reference: date,
    layout_llm: Callable[[str], MenuLayout | None] | None = None,
) -> tuple[list[DayMenu], str]:
    """(요일별 메뉴, 판독 방식 layout|nearest). 글자가 없거나 표를 못 찾으면 ([], "")."""
    try:
        frags = _fragments(pdf)
    except Exception:  # noqa: BLE001 — 깨진 PDF는 빈 결과(원문 링크 안내로)
        return [], ""
    heads = []
    for x, y, t in frags:
        m = DAY_RE.match(t)
        if m:
            month, dd = int(m[1]), int(m[2])
            try:
                heads.append((x, y, date(_year_for(month, reference), month, dd)))
            except ValueError:
                continue
    if len(heads) < 3:
        return [], ""
    head_y = max(y for _, y, _ in heads)
    heads = sorted(h for h in heads if abs(h[1] - head_y) < 5)
    xs = [h[0] for h in heads]
    bounds = [(a + b) / 2 for a, b in zip(xs, xs[1:], strict=False)]
    label_max_x = xs[0] * LABEL_MAX_X_RATIO

    def col(x: float) -> int:
        return sum(1 for b in bounds if x >= b)

    body = [f for f in frags if f[1] < head_y - 3]
    labels = [f for f in body if f[0] < label_max_x and f[2] != "구분" and len(f[2]) <= 20]
    rows: dict[int, list[tuple[float, str]]] = {}
    for x, y, t in body:
        if x < label_max_x or t in PLACEHOLDERS or t.startswith(("*", "<")):
            continue
        # 같은 행이 1~2pt 어긋나는 경우(예: 181·180)를 한 행으로
        key = next((k for k in rows if abs(k - y) <= 2), round(y))
        rows.setdefault(key, []).append((x, t))
    row_ys = sorted(rows, reverse=True)
    if not row_ys:
        return [], ""

    layout = None
    if layout_llm is not None:
        desc = ["[구분 글자]"] + [f"x={round(x)} y={round(y)} {t}" for x, y, t in labels]
        desc += ["[메뉴 행]"] + [
            f"y={y} | " + " / ".join(t for _, t in sorted(rows[y])) for y in row_ys
        ]
        for _ in range(LAYOUT_ATTEMPTS):  # AI 판독은 편차가 있어 한 번 더 시도
            layout = layout_llm(chr(10).join(desc))
            if layout is not None:
                got = sorted(y for s in layout.sections for y in s.row_ys)
                if got == sorted(row_ys):  # 모든 행을 정확히 한 번
                    break
            layout = None
    method = "layout" if layout is not None else "nearest"
    if layout is None:
        layout = _nearest_layout(row_ys, labels)

    ordered = sorted(layout.sections, key=lambda s: -max(s.row_ys or [0]))
    days = []
    for c, (_, _, day) in enumerate(heads):
        sections = []
        for sec in ordered:
            items = [
                t
                for y in sorted(sec.row_ys, reverse=True)
                for x, t in sorted(rows[y])
                if col(x) == c
            ]
            if items:
                sections.append({"name": sec.name.strip(), "items": items})
        days.append(DayMenu(day=day, sections=sections))
    return days, method


class OcrSection(BaseModel):
    name: str
    items: list[str] = Field(default_factory=list)


class OcrDay(BaseModel):
    date: str = Field(description="YYYY-MM-DD")
    sections: list[OcrSection] = Field(default_factory=list)


class OcrWeek(BaseModel):
    days: list[OcrDay] = Field(default_factory=list)


OCR_PROMPT = (
    "대학 식당 주간 식단표(스캔 이미지 PDF)를 판독한다. 날짜별로 끼니·코너 구획과 메뉴를 표에 적힌 그대로 옮겨라. "
    "구획 이름은 '조식 천원의 아침밥', '중식 차림(정식)'처럼 바깥 구분을 앞에 붙인다. "
    "없는 메뉴를 지어내지 말고, 빈 칸과 ♥ 같은 표시는 뺀다."
)


def ocr_days(week: OcrWeek | None) -> list[DayMenu]:
    """스캔 PDF 판독 결과 → DayMenu. 날짜 형식이 틀린 날은 버린다."""
    out = []
    for d in week.days if week else []:
        try:
            day = date.fromisoformat(d.date)
        except ValueError:
            continue
        secs = [
            {
                "name": s.name.strip(),
                "items": [
                    i.strip() for i in s.items if i.strip() and i.strip() not in PLACEHOLDERS
                ],
            }
            for s in d.sections
        ]
        secs = [s for s in secs if s["items"]]
        if secs:
            out.append(DayMenu(day=day, sections=secs))
    return out


CAFETERIAS = (("학생식당", "/ko/4454/subview.do"), ("푸드코트", "/ko/8978/subview.do"))
MAX_WEEKS = 10


class Docs(Protocol):
    def get(self, collection: str, doc_id: str) -> dict[str, Any] | None: ...
    def merge(self, collection: str, doc_id: str, data: dict[str, Any]) -> None: ...
    def find(self, collection: str, field: str, value: Any) -> list[tuple[str, dict[str, Any]]]: ...


REFRESH_WEEKS = 2  # 다음 주 식단이 먼저 올라와도 이번 주 정정을 반영(GPT5 #667-3)


def collect_menus(
    docs: Docs,
    *,
    today: date,
    now: Any,
    list_weeks: Callable[[str], list[dict[str, str]]],
    download: Callable[[str], bytes],
    layout_llm: Callable[[str], MenuLayout | None] | None = None,
    ocr: Callable[[bytes, date], OcrWeek | None] | None = None,
) -> dict[str, int]:
    """학교 식단 페이지의 최근 주간 식단표를 날짜별로 저장.

    상위 REFRESH_WEEKS주는 매번 다시 읽고, 다시 읽은 주에서 빠진 날(휴무·삭제)은 unavailable로 둔다.
    """
    stats = {"weeks": 0, "days": 0, "skipped": 0, "failed": 0}
    for cafeteria, path in CAFETERIAS:
        for i, w in enumerate(list_weeks(path)[:MAX_WEEKS]):
            key = f"{cafeteria}_{w['record_id']}"
            known = docs.get("menu_weeks", key)
            if (
                i >= REFRESH_WEEKS and known is not None and known.get("status") == "ok"
            ):  # 실패한 주는 다시 시도
                stats["skipped"] += 1
                continue
            try:
                pdf = download(w["url"])
                days, method = ([], "")
                if pdf[:4] == b"%PDF":
                    days, method = parse_week_method(pdf, today, layout_llm)
                    if method != "layout" and ocr is not None:
                        ocr_result = ocr_days(
                            ocr(pdf, today)
                        )  # 스캔 PDF·표 판독 실패 → 이미지 판독
                        if ocr_result:
                            days, method = ocr_result, "ocr"
                            stats["ocr"] = stats.get("ocr", 0) + 1
            except Exception:  # noqa: BLE001 — 한 주 실패가 나머지를 막지 않게
                days, method = [], ""
            prev = METHOD_RANK.get((known or {}).get("method", ""), 0)
            if days and known and known.get("status") == "ok" and METHOD_RANK.get(method, 0) < prev:
                stats["kept"] = stats.get("kept", 0) + 1  # 이전 결과가 더 정확 → 유지
                continue
            if not days:
                stats["failed"] += 1
                docs.merge("menu_weeks", key, {"title": w["title"], "status": "failed", "at": now})
                continue
            kept: set[str] = set()
            for dm in days:
                if not dm.sections:
                    continue
                kept.add(f"{cafeteria}_{dm.day.isoformat()}")
                docs.merge(
                    "campus_menus",
                    f"{cafeteria}_{dm.day.isoformat()}",
                    {
                        "cafeteria": cafeteria,
                        "menu_date": dm.day.isoformat(),
                        "sections": dm.sections,
                        "week_title": w["title"],
                        "source_url": w["url"],
                        "source_record_id": key,
                        "status": "ok",
                        "updated_at": now,
                    },
                )
                stats["days"] += 1
            for mid, row in docs.find("campus_menus", "source_record_id", key):
                if mid not in kept and row.get("status") != "unavailable":
                    docs.merge("campus_menus", mid, {"status": "unavailable", "updated_at": now})
                    stats["unavailable"] = stats.get("unavailable", 0) + 1
            docs.merge(
                "menu_weeks",
                key,
                {"title": w["title"], "status": "ok", "method": method, "at": now},
            )
            stats["weeks"] += 1
    return stats


def live_menu_sources(settings: Any):
    """(list_weeks, download) — 학교 식단 페이지의 주간 선택 목록과 첨부 PDF."""
    import asyncio
    from urllib.parse import urljoin

    from bs4 import BeautifulSoup

    from backend.tools import public_sources as ps

    def list_weeks(path: str) -> list[dict[str, str]]:
        html = asyncio.run(ps._fetch(urljoin(ps.BASE_URL, path), settings))
        sel = BeautifulSoup(html, "html.parser").select_one('select[name="selArtclSeq"]')
        if sel is None:
            return []
        g = re.search(
            r"jf_curriculum_chg\('[^']+','(?P<group>\d+)'", str(sel.get("onchange") or "")
        )
        if not g:
            return []
        out = []
        for opt in sel.select("option[value]"):
            rid = str(opt.get("value") or "").strip()
            if rid:
                out.append(
                    {
                        "title": opt.get_text(" ", strip=True),
                        "record_id": rid,
                        "url": urljoin(
                            ps.BASE_URL, f"/curriculum/ko/{g['group']}/{rid}/fileDownload.do"
                        ),
                    }
                )
        return out

    def download(url: str) -> bytes:
        return asyncio.run(ps._fetch(url, settings))

    return list_weeks, download


def live_layout_llm(settings: Any) -> Callable[[str], MenuLayout | None]:
    import asyncio

    from backend.agent.llm import GeminiLLM

    llm = GeminiLLM(settings)

    def run(text: str) -> MenuLayout | None:
        try:
            return asyncio.run(
                llm.structured(MenuLayout, LAYOUT_PROMPT, text, node="act", deadline=None)
            )
        except Exception:  # noqa: BLE001 — 대안 배치로
            return None

    return run


def live_ocr(settings: Any) -> Callable[[bytes, date], OcrWeek | None]:
    """스캔 PDF를 Gemini에 직접 보내 표를 판독(답변 길이 상한을 이 호출만 크게)."""
    import asyncio
    import base64

    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_google_genai import ChatGoogleGenerativeAI

    chat = ChatGoogleGenerativeAI(
        vertexai=True,
        model=settings.gemini_model,
        project=settings.gcp_project_id or None,
        location=settings.gemini_location,
        temperature=0,
        max_output_tokens=8192,
        max_retries=1,
        thinking_level="low",
    ).with_structured_output(OcrWeek)

    def run(pdf: bytes, today: date) -> OcrWeek | None:
        msg = [
            SystemMessage(OCR_PROMPT + f" 연도는 {today.year}년 기준으로 판단한다."),
            HumanMessage(
                content=[
                    {"type": "text", "text": "이 식단표를 판독해 주세요."},
                    {
                        "type": "file",
                        "source_type": "base64",
                        "mime_type": "application/pdf",
                        "data": base64.b64encode(pdf).decode(),
                    },
                ]
            ),
        ]
        try:
            return asyncio.run(asyncio.wait_for(chat.ainvoke(msg), timeout=120))
        except Exception:  # noqa: BLE001 — 판독 실패는 원문 링크 안내로
            return None

    return run


def menu_text(cafeteria: str, dm: DayMenu) -> str:
    lines = [f"{dm.day.isoformat()} {cafeteria} 식단"]
    for s in dm.sections:
        lines.append(f"- {s['name']}: {', '.join(s['items'])}")
    return "\n".join(lines)

"""결정적 인용 검증(상세설계 02 §4-7) — LLM 호출 없음.

규칙
1. 인용 ID가 이번 턴 evidence에 있어야 한다.
2. supporting_quotes가 근거 본문에 정규화 후 그대로 들어 있어야 한다(cite_ids와 길이 일치).
3. 단위가 붙은 숫자는 인용 근거에 있어야 한다(학생 조건 값은 허용, 인용 표기는 제외).
4. checklist·next_actions도 같은 검사를 받는다.

부정·양태 반전은 삭제가 아니라 ReviewFlag로 표시한다. 좁은 반전만 문장을 숨긴다(suppress_sentence).
"""

from __future__ import annotations

import re
import unicodedata

from backend.domain.answer import (
    Draft,
    DraftSentence,
    DroppedSentence,
    Resolution,
    ReviewFlag,
    VerifyReport,
)
from backend.domain.evidence import Evidence
from backend.domain.thread import Profile

MIN_QUOTE_LEN = 6

NUM_FACT = re.compile(
    r"\d+(?:[.,]\d+)?\s*(?:학점|년|월|일|주일?|개월|학기|회|번|차례|%|퍼센트|원|만\s*원|시간|분|명|과목)"
)
CITATION_MARK = re.compile(r"제\s*\d+\s*(?:조(?:\s*의\s*\d+)?|항|호|장|절|편)")
_STRIP = re.compile(r"[\s\W_]+", re.UNICODE)
ISO_DATE = re.compile(r"(\d{4})\s*[-./]\s*(\d{1,2})\s*[-./]\s*(\d{1,2})")
# 연도 없는 공지 표기 "9. 29." — 끝 점까지 있어야 날짜로 본다(소수 "1.50"과 구분)
MONTH_DAY = re.compile(r"(?<![\d.])(\d{1,2})\.\s*(\d{1,2})\.(?!\d)")


def date_variants(text: str) -> str:
    """근거의 ISO 날짜(2026-10-08)를 한국어 표기(10월8일 등)로도 대조되게 덧붙인다.

    학사일정 도구는 ISO로, 모델은 "10월 8일"로 쓴다(2026-09-29 실측: 전 문장 number_mismatch).
    값은 그대로이고 표기만 늘리므로 없는 숫자를 허용하지 않는다.
    """
    extra = []
    for y, m, d in ISO_DATE.findall(text):
        mo, da = int(m), int(d)
        extra.append(f"{y}년{mo}월{da}일 {mo}월{da}일 {mo}월 {da}일 {y}년")
    for m, d in MONTH_DAY.findall(text):
        mo, da = int(m), int(d)
        if 1 <= mo <= 12 and 1 <= da <= 31:
            extra.append(f"{mo}월{da}일 {mo}월 {da}일")
    return text + (" " + " ".join(extra) if extra else "")


# 표 행 "[표] 머리글: 값 · …"(backend/ingest/rules.render_table)의 숫자 칸에 머리글의 단위를 붙인다.
# "졸업학점: 120" → "120학점", "입학정원: 30" → "30명"(교수님 2026-10-01 #883)
TABLE_PAIR = re.compile(r"([^:·|\]]+?)\s*:\s*([￦₩]?)\s*(\d+(?:[.,]\d+)*)\s*(?=·|$)")
# 원화 기호 "￦1,000"(홈페이지 요금표)은 답변의 "1,000원"과 같은 값이다(#904-4)
WON_SIGN = re.compile(r"[￦₩]\s*(\d+(?:,\d{3})*)")
LABEL_UNITS = (
    ("학점", "학점"),
    ("개월", "개월"),
    ("학기", "학기"),
    ("년", "년"),
    ("시간", "시간"),
    ("정원", "명"),
    ("인원", "명"),
    ("금액", "원"),
    ("등록금", "원"),
    ("장학금", "원"),
    ("요금", "원"),
    ("수수료", "원"),
    ("비율", "%"),
    ("횟수", "회"),
)


def table_variants(text: str) -> str:
    """값은 그대로 두고 표기만 늘린다 — 근거 행에 없는 숫자는 여전히 허용하지 않는다."""
    extra = []
    for line in text.splitlines():
        if not line.startswith("[표]"):
            continue
        for label, won, value in TABLE_PAIR.findall(line[4:]):
            if won:
                extra.append(f"{value}원")
                continue
            for key, unit in LABEL_UNITS:
                if key in label.replace(" ", ""):
                    extra.append(f"{value}{unit}")
                    break
    extra += [f"{v}원" for v in WON_SIGN.findall(text)]
    return text + (" " + " ".join(extra) if extra else "")


def normalize(t: str) -> str:
    """NFKC → 공백·문장부호·따옴표 제거 → 소문자."""
    return _STRIP.sub("", unicodedata.normalize("NFKC", t)).lower()


def profile_numbers(p: Profile) -> list[tuple[int, str]]:
    return [(p.grade, "학년")] if p.grade is not None else []


def check_sentence(s: DraftSentence, ev: dict[str, Evidence], profile: Profile) -> str | None:
    """통과하면 None, 탈락이면 사유 코드."""
    if not s.cite_ids:
        return "no_cite"
    if not s.supporting_quotes:
        return "len_mismatch"
    if any(cid not in ev for cid in s.cite_ids):
        return "unknown_id"
    if len(s.cite_ids) == len(s.supporting_quotes):
        pairs = [
            (normalize(ev[cid].text), q)
            for cid, q in zip(s.cite_ids, s.supporting_quotes, strict=True)
        ]
    else:
        # 근거 여러 개에 인용문 수가 어긋나도(모델 형식 실수) 문장을 버리지 않는다 —
        # 인용문마다 인용한 근거들 중 어딘가에 그대로 있어야 한다(밀양 통학버스 요금, 2026-10-01)
        union = normalize("\n".join(ev[c].text for c in s.cite_ids))
        pairs = [(union, q) for q in s.supporting_quotes]
    for text, q in pairs:
        nq = normalize(q)
        if len(nq) < MIN_QUOTE_LEN:
            return "quote_too_short"
        if nq not in text:
            return "quote_not_found"
    cited = "\n".join(ev[c].text for c in s.cite_ids)
    cited_text = normalize(table_variants(date_variants(cited)))
    allowed = {normalize(f"{v}{u}") for v, u in profile_numbers(profile)}
    body = CITATION_MARK.sub(" ", s.text)
    for m in NUM_FACT.finditer(body):
        n = normalize(m.group(0))
        if n in allowed:
            continue
        if n not in cited_text:
            return "number_mismatch"
    return None


# ── 부정·양태 반전 표시 ───────────────────────────────────────────────
NEGATION = ("할 수 없", "하지 아니", "불가")
OBLIGATION, PERMISSION = "하여야", "할 수 있"
RISKY = (
    "아니",
    "않",
    "없",
    "못",
    "불가",
    "제외",
    "금지",
    "그러하지",
    "단,",
    "다만",
    "하여야",
    "할 수",
    # 어휘 자체로 극성이 갈리는 서술어(Gemini #526 리뷰) — 오탐이 많아 내부 표시만
    "제한",
    "허용",
    "허가",
    "인정",
)
_STEM = re.compile(r"([가-힣]{2,})\s*$")


def _stem_before(text: str, marker: str) -> str | None:
    i = text.find(marker)
    if i < 0:
        return None
    m = _STEM.search(text[:i])
    if not m:
        return None
    word = m.group(1)
    return word[:-1] if len(word) > 2 else word  # 활용 어미 한 글자 여유


def _narrow_flip(sentence: str, quotes: str) -> bool:
    """좁은 반전: 같은 서술어 어간을 공유하며 한쪽만 부정이거나, 의무↔가능이 뒤바뀐 경우."""
    for neg in NEGATION:
        a, b = neg in sentence, neg in quotes
        if a != b:
            stem = _stem_before(sentence if a else quotes, neg)
            other = quotes if a else sentence
            if stem and stem in other:
                return True

    def only(tok: str, x: str, y: str) -> bool:
        return tok in x and tok not in y

    return (only(OBLIGATION, sentence, quotes) and only(PERMISSION, quotes, sentence)) or (
        only(PERMISSION, sentence, quotes) and only(OBLIGATION, quotes, sentence)
    )


def _risky_mismatch(sentence: str, quotes: str) -> bool:
    return any((tok in sentence) != (tok in quotes) for tok in RISKY)


def polarity_flag(s: DraftSentence, index: int) -> ReviewFlag | None:
    quotes = " ".join(s.supporting_quotes)
    if _narrow_flip(s.text, quotes):
        return ReviewFlag(
            code="polarity_flip",
            severity="high",
            source_ids=list(s.cite_ids),
            sentence_index=index,
            public_action="suppress_sentence",
        )
    if _risky_mismatch(s.text, quotes):
        return ReviewFlag(
            code="risky_token",
            severity="low",
            source_ids=list(s.cite_ids),
            sentence_index=index,
            public_action="internal_only",
        )
    return None


SUPPRESSED_TEMPLATE = (
    "일부 내용은 근거 해석 확인이 필요해 표시하지 않았습니다. 원문을 확인해 주세요."
)


def verify(
    draft: Draft,
    evidence: list[Evidence],
    profile: Profile,
    resolution: Resolution | None = None,
) -> tuple[Draft, VerifyReport, list[ReviewFlag]]:
    ev = {e.id: e for e in evidence}
    dropped: list[DroppedSentence] = []
    flags: list[ReviewFlag] = []

    def keep(section: str, items: list[DraftSentence]) -> list[DraftSentence]:
        out = []
        for i, s in enumerate(items):
            reason = check_sentence(s, ev, profile)
            if reason is None:
                flag = polarity_flag(s, i) if section == "sentences" else None
                if flag:
                    flags.append(flag)
                    if flag.public_action == "suppress_sentence":
                        reason = "suppressed"
            if reason is None:
                out.append(s)
            else:
                dropped.append(DroppedSentence(section=section, index=i, reason=reason))
        return out

    kept = keep("sentences", draft.sentences)
    kept_cl = keep("checklist", draft.checklist)
    kept_na = keep("next_actions", draft.next_actions)

    if resolution is not None:
        cited = {c for s in kept + kept_cl + kept_na for c in s.cite_ids}
        for ids in resolution.adopted.values():
            if ids and not cited.intersection(ids):
                flags.append(
                    ReviewFlag(
                        code="adopted_not_cited",
                        severity="medium",
                        source_ids=list(ids),
                        public_action="internal_only",
                    )
                )

    report = VerifyReport(total=len(draft.sentences), kept=len(kept), dropped=dropped)
    verified = draft.model_copy(
        update={"sentences": kept, "checklist": kept_cl, "next_actions": kept_na}
    )
    return verified, report, flags

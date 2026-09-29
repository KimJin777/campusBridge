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

from backend.domain.answer import Draft, DraftSentence, DroppedSentence, Resolution, ReviewFlag, VerifyReport
from backend.domain.evidence import Evidence
from backend.domain.thread import Profile

MIN_QUOTE_LEN = 6

NUM_FACT = re.compile(
    r"\d+(?:[.,]\d+)?\s*(?:학점|년|월|일|주일?|개월|학기|회|번|차례|%|퍼센트|원|만\s*원|시간|분|명|과목)"
)
CITATION_MARK = re.compile(r"제\s*\d+\s*(?:조(?:\s*의\s*\d+)?|항|호|장|절|편)")
_STRIP = re.compile(r"[\s\W_]+", re.UNICODE)


def normalize(t: str) -> str:
    """NFKC → 공백·문장부호·따옴표 제거 → 소문자."""
    return _STRIP.sub("", unicodedata.normalize("NFKC", t)).lower()


def profile_numbers(p: Profile) -> list[tuple[int, str]]:
    return [(p.grade, "학년")] if p.grade is not None else []


def check_sentence(s: DraftSentence, ev: dict[str, Evidence], profile: Profile) -> str | None:
    """통과하면 None, 탈락이면 사유 코드."""
    if not s.cite_ids:
        return "no_cite"
    if len(s.cite_ids) != len(s.supporting_quotes):
        return "len_mismatch"  # zip이 조용히 자르는 것 방지
    if any(cid not in ev for cid in s.cite_ids):
        return "unknown_id"
    for cid, q in zip(s.cite_ids, s.supporting_quotes):
        nq = normalize(q)
        if len(nq) < MIN_QUOTE_LEN:
            return "quote_too_short"
        if nq not in normalize(ev[cid].text):
            return "quote_not_found"
    cited_text = normalize(" ".join(ev[c].text for c in s.cite_ids))
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
RISKY = ("아니", "않", "없", "못", "불가", "제외", "금지", "그러하지", "단,", "다만", "하여야", "할 수")
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
    """문장과 인용 구간이 같은 서술어 어간을 공유하면서 한쪽만 부정이거나, 의무↔가능이 뒤바뀐 경우."""
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
            code="polarity_flip", severity="high", source_ids=list(s.cite_ids),
            sentence_index=index, public_action="suppress_sentence",
        )
    if _risky_mismatch(s.text, quotes):
        return ReviewFlag(
            code="risky_token", severity="low", source_ids=list(s.cite_ids),
            sentence_index=index, public_action="internal_only",
        )
    return None


SUPPRESSED_TEMPLATE = "일부 내용은 근거 해석 확인이 필요해 표시하지 않았습니다. 원문을 확인해 주세요."


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
        for need, ids in resolution.adopted.items():
            if ids and not cited.intersection(ids):
                flags.append(ReviewFlag(
                    code="adopted_not_cited", severity="medium", source_ids=list(ids),
                    public_action="internal_only",
                ))

    report = VerifyReport(total=len(draft.sentences), kept=len(kept), dropped=dropped)
    verified = draft.model_copy(update={"sentences": kept, "checklist": kept_cl, "next_actions": kept_na})
    return verified, report, flags

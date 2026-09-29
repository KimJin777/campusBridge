"""교내 전화번호부 → 부서 대표 연락처(교수님 2026-09-29: 조직도처럼 사용, 업로드 시 자동 적용).

- 입력: 학교가 배포하는 전화번호부(HWP 권장 — hwp5txt로 번호까지 정확, PDF는 표 번호가 빠질 수 있음)
- 부서 줄(￭·¤·▣ 등으로 시작)과 사람 이름이 없는 시설 줄("교원임용심사실 2448")만 쓴다
- **직원 이름은 저장하지 않는다.** 부서 줄에 번호가 없으면 바로 아래 첫 내선번호(보통 부서장 자리)를
  대표 번호로 쓰고 `basis="first_listed"`로 표시한다
- 내선 4자리는 055-249-XXXX, 3~4자리-4자리는 055-XXX-XXXX로 정규화, (F)는 팩스
"""

from __future__ import annotations

import re
from dataclasses import dataclass

UNIT_MARK = re.compile(r"^\s*([￭¤▣■◆●]|[\U000F0000-\U000FFFFF])\s*")
NUM = re.compile(
    r"(0505-\d{3}-\d{4}|0\d{1,2}-\d{3,4}-\d{4}|\d{3,4}-\d{4}|(?<![\d-])\d{4}(?![\d-]))(\(F\))?"
)
UNIT_SUFFIX = (
    "팀",
    "실",
    "센터",
    "소",
    "과",
    "관",
    "단",
    "원",
    "처",
    "부",
    "국",
    "회",
    "학부",
    "본부",
    "대학원",
    "사",
)
TITLES = (
    "팀장",
    "소장",
    "실장",
    "과장",
    "원장",
    "단장",
    "부장",
    "처장",
    "국장",
    "관장",
    "센터장",
    "학장",
    "부단장",
    "분소장",
    "회장",
    "부회장",
    "사무국장",
    "이사장",
    "총장",
    "연구원",
    "조교",
    "전담직원",
    "직원",
)
GENERIC = {"부속실", "사무실", "교학행정실", "행정실"}
HANGUL_NAME = re.compile(r"^[가-힣]{2,4}$")
PRIVATE_USE = re.compile(r"[-󰀀-󿿿]")
# 시설 줄 이름은 사람 이름("서지원", "최동원")과 헷갈리지 않도록 뚜렷한 접미사 + 4자 이상만
FACILITY_SUFFIX = (
    "실",
    "센터",
    "팀",
    "연구소",
    "위원회",
    "추진단",
    "사업단",
    "지원단",
    "본부",
    "학부",
    "학과",
    "진료소",
    "상담소",
    "도서관",
    "생활관",
    "사무국",
)


def clean(name: str) -> str:
    return PRIVATE_USE.sub("", name).replace("", "·").strip()


@dataclass
class DirectoryEntry:
    name: str
    parent: str | None
    phone: str | None
    fax: str | None
    basis: str  # unit_line | first_listed | facility_line


def normalize_number(raw: str) -> str:
    raw = raw.strip()
    if re.fullmatch(r"\d{4}", raw):
        return f"055-249-{raw}"
    if re.fullmatch(r"\d{3,4}-\d{4}", raw):
        return f"055-{raw}"
    return raw


def _numbers(text: str) -> tuple[list[str], list[str]]:
    phones, faxes = [], []
    for m in NUM.finditer(text):
        (faxes if m.group(2) else phones).append(normalize_number(m.group(1)))
    return phones, faxes


def _strip_numbers(text: str) -> str:
    return NUM.sub(" ", text).replace("·", " ").strip()


def _unit_name(rest: str) -> str | None:
    """ "환경문제연구소장 이원제" → "환경문제연구소", "처장 권영훈" → None(사람 머리글)."""
    tokens = _strip_numbers(rest).split()
    if not tokens:
        return None
    head = tokens[0]
    if head in TITLES or any(
        head.startswith(t) and HANGUL_NAME.match(head[len(t) :] or "x") for t in TITLES
    ):
        return None  # "처장 권영훈", "팀장김혜진"
    if head.endswith("장") and len(tokens) > 1 and HANGUL_NAME.match(tokens[1]):
        head = head[:-1]
    if head.startswith("부설") and len(tokens) > 1:
        head = tokens[1]
    head = clean(head)
    return (
        head
        if len(head) >= 2 and not HANGUL_NAME.fullmatch(head) or head.endswith(UNIT_SUFFIX)
        else None
    )


def _is_person(text: str) -> bool:
    tokens = _strip_numbers(text).replace("(", " ").split()
    if not tokens:
        return True
    first = tokens[0]
    if first in TITLES or any(first.startswith(t) for t in TITLES):
        return True
    return bool(HANGUL_NAME.match(first)) and not first.endswith(UNIT_SUFFIX)


def parse_phonebook(text: str) -> list[DirectoryEntry]:
    entries: dict[str, DirectoryEntry] = {}
    parent: str | None = None
    current: DirectoryEntry | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        m = UNIT_MARK.match(line)
        if m:
            rest = line[m.end() :]
            name = _unit_name(rest)
            if name is None:
                continue
            phones, faxes = _numbers(rest)
            # 같은 줄에 "팀장박승원 2397"이 붙어 오는 경우 — 부서 번호로 보지 않는다
            unit_part = re.split(r"\s{2,}", rest.strip())[0]
            unit_phones, _ = _numbers(unit_part)
            entry = DirectoryEntry(
                name,
                parent,
                unit_phones[0] if unit_phones else None,
                faxes[0] if faxes else None,
                "unit_line",
            )
            if not unit_phones and phones:
                entry.phone, entry.basis = phones[0], "first_listed"
            if m.group(1) in ("¤", "▣"):
                parent = name
            entries.setdefault(name, entry)
            current = entries[name]
            continue
        phones, _ = _numbers(line)
        if not phones:
            stripped = line.strip()
            if stripped.endswith(UNIT_SUFFIX) and len(stripped) <= 20 and " " not in stripped:
                parent = clean(stripped)  # "산학협력단" 같은 구획 머리글
            continue
        body = _strip_numbers(line)
        if _is_person(body):
            if current is not None and current.phone is None:
                current.phone, current.basis = phones[0], "first_listed"
            continue
        name = clean(body.split()[0]) if body.split() else ""
        if name and name.endswith(FACILITY_SUFFIX) and name not in GENERIC and len(name) >= 4:
            entries.setdefault(
                name,
                DirectoryEntry(
                    name, current.name if current else parent, phones[0], None, "facility_line"
                ),
            )
    return list(entries.values())


def entry_id(name: str) -> str:
    """장소 표 조직 행과 같은 ID 규칙(u-{sha1}) — 전화번호부와 위치 정보를 이름으로 잇는다."""
    import hashlib

    return f"u-{hashlib.sha1(name.encode('utf-8')).hexdigest()[:12]}"


def apply_directory(
    entries: list[DirectoryEntry], db, *, source: str, actor: str
) -> dict[str, int]:
    """Firestore `directory_entries`를 이번 전화번호부로 **통째로 교체**(관리자 업로드 = 적용 승인).

    공식 문서를 관리자가 올린 것이므로 별도 검수 없이 즉시 적용한다. 직원 이름은 저장하지 않는다.
    """
    import datetime as dt

    now = dt.datetime.now(dt.UTC)
    col = db.collection("directory_entries")
    new_ids = {entry_id(e.name): e for e in entries}
    old_ids = {d.id for d in col.select([]).stream()}
    batch, n = db.batch(), 0

    def flush():
        nonlocal batch, n
        if n:
            batch.commit()
        batch, n = db.batch(), 0

    for pid, e in new_ids.items():
        batch.set(
            col.document(pid),
            {
                "name": e.name,
                "parent": e.parent,
                "phone": e.phone,
                "fax": e.fax,
                "basis": e.basis,
                "source": source,
                "applied_at": now,
                "applied_by": actor,
            },
        )
        n += 1
        if n >= 400:
            flush()
    removed = old_ids - set(new_ids)
    for pid in removed:
        batch.delete(col.document(pid))
        n += 1
        if n >= 400:
            flush()
    flush()
    stats = {
        "entries": len(new_ids),
        "with_phone": sum(1 for e in entries if e.phone),
        "added": len(set(new_ids) - old_ids),
        "removed": len(removed),
    }
    db.collection("source_configs").document("phonebook").set(
        {
            "kind": "phonebook",
            "last_success_at": now,
            "doc_count": stats["entries"],
            "source": source,
            "last_stats": stats,
            "status": "applied",
        },
        merge=True,
    )
    return stats

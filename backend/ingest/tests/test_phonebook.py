from __future__ import annotations

from backend.ingest.job import JobDeps, process_phonebook
from backend.ingest.phonebook import normalize_number, parse_phonebook
from backend.ingest.tests.test_job import FakeBlobs, FakeDocs, FakeIndex

BOOK = """2026학년도 경남대학교전화번호부
■대표전화: 245-5000
¤총장실
  총장 홍길동 2001·246-6228
￭비서실 246-1634(F)
  실장 김철수 2005·246-7714
        서지원 2362
▣교무부 0505-999-2106(F)
￭학사관리팀 242-5651(F)
  팀장김혜진 2023
       서인순 2961
 교원임용심사실 2448
￭산학기획팀 0505-999-2127(F)  팀장박승원 2397
￭환경문제연구소장 이원제 2212
￭법인행정실 246-5314·247-9594(F)
"""


def test_units_numbers_and_no_person_names():
    es = {e.name: e for e in parse_phonebook(BOOK)}
    assert es["학사관리팀"].phone == "055-249-2023" and es["학사관리팀"].basis == "first_listed"
    assert es["학사관리팀"].fax == "055-242-5651" and es["학사관리팀"].parent == "교무부"
    assert es["산학기획팀"].phone == "055-249-2397"  # 같은 줄 부서장 번호, 이름은 버림
    assert es["환경문제연구소"].phone == "055-249-2212"  # "소장 이름" 제거
    assert es["법인행정실"].phone == "055-246-5314" and es["법인행정실"].basis == "unit_line"
    assert (
        es["교원임용심사실"].phone == "055-249-2448"
        and es["교원임용심사실"].basis == "facility_line"
    )
    assert es["비서실"].phone == "055-249-2005"
    names = set(es)
    assert not names & {"서지원", "김혜진", "서인순", "박승원", "이원제", "홍길동", "김철수"}
    assert "총장" not in names  # 사람 머리글


def test_number_normalization():
    assert normalize_number("2023") == "055-249-2023"
    assert normalize_number("246-5314") == "055-246-5314"
    assert normalize_number("0505-999-2106") == "0505-999-2106"


def _deps(files):
    return JobDeps(blobs=FakeBlobs(files), docs=FakeDocs({}), index=FakeIndex())


def test_job_applies_directory_and_rejects_bad_files():
    applied = {}

    def apply(entries, source):
        applied["n"], applied["source"] = len(entries), source
        return {"entries": len(entries)}

    big = BOOK + "\n".join(f"￭테스트{i}팀 {2100 + i}" for i in range(30))
    out = process_phonebook("x/book.txt", _deps({"x/book.txt": big.encode()}), apply)
    assert out["status"] == "applied" and applied["source"] == "x/book.txt"

    d = _deps({"x/book.txt": BOOK.encode()})  # 너무 적음 → 기존 전화번호부 유지
    assert process_phonebook("x/book.txt", d, apply)["error_code"] == "TOO_FEW_ENTRIES"
    assert d.docs.docs["phonebook"]["status"] == "failed"
    assert process_phonebook("x/none.hwp", _deps({}), apply)["error_code"] == "SOURCE_MISSING"
